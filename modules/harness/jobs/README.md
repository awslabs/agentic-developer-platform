# Harness Jobs — durable operation store and transactional dispatch outbox

Issue [#5525](https://github.com/aws-e/adp/issues/5525) (w6-02), EPIC #4910, Wave 6.
Implements the `operation_facade` port declared in
[#5524](https://github.com/aws-e/adp/issues/5524)'s registry
(`superplane_contracts.integration`, owner `harness_jobs`).

An accepted operation must not be lost, must not be run twice, and must not be
visible to another tenant. This package is where those three properties are made true,
once, for every consumer that opens a long-running operation.

## What it is, and what it is not

It is **a library**. It opens no connection, reads no DSN, and holds no credential.
Every entry point is handed an already-open connection by whoever composes it, which is
why the package declares no runtime dependencies at all — not even a driver.

That is a deliberate constraint rather than an accident of packaging. If this package
could build its own pool, "is this port installed, and against which database" would be
a property of this directory instead of a property of the reviewed startup sequence that
installs it. It also means there is no code path here that could log, echo or leak a
connection string, because there is none to leak.

Consequently **"deploy" does not mean "start a service"** — see
[Deployment](#deployment) below. Nothing composes this package yet; #5535 (w6-12) is the
story that installs it into the Superplane API.

## The three guarantees

### 1. Admission and enqueue commit together

`OperationStore.admit()` writes the operation record and its outbox row in **one
transaction against one database**. The commit is the durability point: before it,
neither row exists; after it, both do. There is no window in which an operation exists
that will never be dispatched, or a dispatch exists for an operation that was never
admitted.

One participant is the whole of "no distributed transactions" — a single store needs no
coordinator, no two-phase commit and no saga, because there is nothing to coordinate
with. That is the reason the outbox is a table here and not a queue service.

### 2. A retry returns the same operation; a changed payload is refused

Idempotency is a `UNIQUE (org_id, workspace_id, idempotency_key)` constraint, and the
insert relies on the conflict rather than checking first. A SELECT-then-INSERT is green
under test and wrong under concurrency: two callers both see nothing and both insert.
Here, the conflict *is* the detection.

The tenant is **inside** the key. A globally unique key would let one tenant's derived
name (`sp-aws-a100-1`, which contains no tenant at all) collide with another's and
refuse an operation that has nothing to do with it — a cross-tenant denial of service
through a naming coincidence.

A retry carrying a *different* payload under an accepted key is refused, detected via a
stored SHA-256 digest. Honouring it would be a silent change to what was accepted —
this is the "same key, bigger machine" case, where the difference is money. Answering it
with the original record would be worse: it tells the caller its *new* request was
accepted.

The job and attempt identity — `job_id`, `attempt_id` — is persisted on both the
operation and its outbox row, and is stable across an idempotent retry because the retry
returns the *stored* row. The published budget hooks are idempotent on
`(job_id, attempt_id)` (`INTEGRATION-CONTRACT.md:296,368`, per #4912), so an unstable job
id would reserve spend twice against two keys for one admitted operation.

### 2b. An admitted operation can be replayed, not merely identified

The request is stored, not only digested. A digest answers "is this the same request?",
which is what idempotency needs and the opposite of what recovery needs: a dispatcher
restarting after a crash must be able to *perform* the work, and a one-way hash cannot
produce it. `OperationRecord.admitted_request()` returns the exact `OperationRequest`
that was admitted.

The digest is still stored beside the payload, and every read re-verifies that they
agree. They were committed in the same transaction, so a disagreement means one was
altered afterwards — a bad migration, a manual `UPDATE` — and the read raises
`ContractViolation` rather than handing back a request nobody admitted under an operation
id that vouches for it. An unexecutable operation is recoverable by an operator; a
silently substituted one is not.

### 3. Delivery is resumable and duplicate-safe

Workers claim with `FOR UPDATE SKIP LOCKED` under an expiring `claimed_until` lease, so
a fleet divides the work instead of queueing on it, and a worker that dies holding a
claim does not strand its row.

Settlement is **last**: deliver, then mark. A crash in between replays the delivery,
which converts an unrecoverable loss into an already-solved duplicate problem — the
store's uniqueness constraint absorbs duplicates and nothing absorbs a loss. The
delivery contract is therefore **at-least-once**, and that is a deliberate choice, not
a limitation.

Workers receive a `DispatchEnvelope` — a frozen dataclass of strings and ints. No
connection, no DSN, no provider secret. A test asserts this structurally on the
dataclass rather than on an instance, so adding a `dsn` field fails the suite instead of
passing review.

Every settlement must present the `claim_generation` it was handed. The generation is a
monotonic counter on the row, incremented by each claim, and it is what makes claim
staleness **decidable** rather than inferred: two workers' clocks can disagree about
whose lease is live, a counter on the row cannot. Without it, a worker that stalled past
its lease could mark a row delivered that its successor was still delivering, or
conclude an operation the successor was actively working. `_mark_delivered`,
`_record_failure` and `_mark_undeliverable` all return a boolean — `False` means "your
claim was superseded, you settled nothing" — and the drain counters only move when a
settlement actually landed, so a caller is never told the queue is emptier than it is.

### A crashed final claim: `recover_abandoned()`

`claim()` increments `attempts` *before* delivery, so a worker killed mid-delivery on
its **last** attempt leaves a row the claim query will not hand out again — attempts are
at the cap — and that nothing else was watching. The operation would stay `PENDING`
forever: accepted work that no process will ever conclude.

`DispatchOutbox.recover_abandoned(connection)` is the second look. It claims rows at the
cap whose lease has expired and which were never settled, and records a durable
`UNKNOWN` with an explanation. Run it on a slower schedule than `drain()` — it is a
maintenance sweep, not part of the hot path.

It *settles* the abandoned claim rather than making the row retryable. Counting only
completed failures instead would leave a row whose delivery kills its worker retrying
forever at the head of the queue; settling bounds the work.

## Identity comes from the authenticated context

`OperationRequest` has **no tenant fields**. It is structurally incapable of carrying
one. `ResolvedPrincipal` is the only carrier of `org_id` / `workspace_id`, and it is
produced by the composer's `PrincipalResolver` from verified request context.

Beyond that, the request *refuses* parameter keys that look like identity assertions
(`org_id`, `tenant`, `principal`, `on_behalf_of`, `role`, `permission`, and the `x-`,
`adp_`, `auth_`, `caller_` prefixes, among others) — so a caller cannot smuggle a tenant
through the one free-form field that remains. The refused set is a documented
**superset** of the domain contract's, since the store must be the stricter side, and
`tests/test_contract_agreement.py` drives its probes from the domain's own constants
rather than a hand-written list. That is what caught `username` being accepted here
while the domain refused it.

`OperationFacadeService.open_operation` also takes `org_id`/`workspace_id` arguments,
because the consumer's Protocol has them. It treats them as **an assertion to be
checked**: if the resolved principal disagrees, the operation is refused rather than
silently performed against the resolved tenant. A mismatch should be a visible error,
not a request that quietly operated on a different workspace than the caller named.

`report_progress(operation_id)` has nowhere to put a tenant — the Protocol declares one
parameter — so it resolves the acting principal through the same resolver and scopes the
read in the `WHERE` clause. An operation in another tenant raises `OperationUnavailable`,
the same answer as one that does not exist, because a distinguishable "exists but
forbidden" reply confirms another tenant's operation exists. An unresolvable principal is
`OperationRefused`: the request reached a working facade and was not entitled to an
answer.

`report_progress_for(principal, operation_id)` is the same read for callers that already
hold a resolved principal. Both are scoped; the unscoped form does not exist.

## `UNKNOWN` is not failure

An operation that could not be delivered, or whose outcome cannot be established, ends
as `UNKNOWN` — terminal, but neither success nor failure. Collapsing it into `FAILED`
either retries a provision that may already be running (duplicate spend) or releases a
reservation for resources that exist. Undeliverability is not evidence about what a
provider did.

## Layout

| Path | Contents |
|------|----------|
| `harness_jobs/identity.py` | `OperationRequest`, `ResolvedPrincipal`, `payload_digest`, `encode_payload`/`decode_payload`, size limits, the forbidden-key set, `CONTRACT_VERSION` |
| `harness_jobs/schema.py` | DDL, `SCHEMA_VERSION`, `apply`, `downgrade`, `current_version`, `check_schema_version` |
| `harness_jobs/store.py` | `OperationStore`: `admit`, `get`, `get_by_idempotency_key`, `list_for_tenant`, `transition` |
| `harness_jobs/outbox.py` | `DispatchOutbox`: `claim`, `drain_once`, `drain`, `pending_count`, `recover_abandoned`; `DispatchEnvelope`, `DispatchExecutor` |
| `harness_jobs/facade.py` | `OperationFacadeService`, the declared port surface; `PORT_REFUSAL_NAMES` |

### Bounded request sizes

Finite by construction, enforced in `OperationRequest.__post_init__`:

| Limit | Value |
|-------|-------|
| `MAX_IDEMPOTENCY_KEY_LENGTH` | 200 |
| `MAX_PARAMETER_COUNT` | 50 |
| `MAX_PARAMETER_KEY_LENGTH` | 100 |
| `MAX_PARAMETER_VALUE_LENGTH` | 2000 |
| `MAX_TOTAL_PARAMETER_BYTES` | 16384 |

## Tests

```bash
cd modules/harness/jobs
pip install -e ".[dev]"
python -m pytest tests -v
```

The database comes from `HARNESS_JOBS_TEST_POSTGRES_URL` if set, and otherwise from the
`pgserver` package, which runs a real PostgreSQL binary against a temporary directory
over a unix socket — no Docker, no root, no listening port. Each test isolates itself in
a randomly named schema. `pgserver` publishes wheels for **Python ≤ 3.12** only.

With neither source, the real-database tests **skip**, and the offline half (contract
agreement, identity validation, digests, size limits) still runs. Skipping is honest;
substituting a fake would not be. SQLite in particular has no `SKIP LOCKED` and
different constraint timing, so a green run against it would be evidence about the fake
rather than about this package.

Set `HARNESS_JOBS_REQUIRE_POSTGRES=1` to turn that skip into a **failure**. CI sets it.
A skip is the right answer for a developer without a database and the wrong answer for a
lane, because "56 skipped" is green and a lane reporting green while asserting none of
these guarantees is worse than no lane.

CI: [`.github/workflows/harness-jobs-ci.yml`](../../../.github/workflows/harness-jobs-ci.yml),
job **`Harness jobs store tests`**. It lints, runs the suite against a real server, and
then asserts the run was not vacuous — a floor on the executed count, because the exit
code alone cannot distinguish "everything passed" from "the database half never ran".

## Deployment

This package ships no service and no Terraform. Deploying it means, for whoever composes
it (#5535):

1. **Install the schema.** `await apply(connection)` — idempotent, so a retried
   install is safe. It creates `harness_operations`, `harness_dispatch_outbox` and
   `harness_jobs_schema_version`.
2. **Check compatibility at startup.** `await check_schema_version(connection)`, or
   `OperationStore.ensure_compatible()`. It refuses to run against a schema older *or*
   newer than `SCHEMA_VERSION`, and says which direction to move.
3. **Supply the two seams.** A `connect` callable returning an async context manager
   yielding a connection, and a `PrincipalResolver` that resolves identity from verified
   request context. Both belong to the composition root; neither can be defaulted here.
4. **Translate the refusals.** The port declares `ProvisioningUnavailable` /
   `ProvisioningRefused`, which live in `app.services.provisioning` — a module this
   package must not import. `PORT_REFUSAL_NAMES` publishes the mapping for a thin
   adapter at the seam. Left untranslated, the conformance suite sees an exception it did
   not declare and reads it as a crash rather than as this port's answer.
5. **Run a delivery loop.** `DispatchOutbox.drain()` on a schedule or a worker. Until
   something drains it, admitted operations stay durably pending — which is the correct
   failure mode, but it is not "working".
6. **Run the recovery sweep.** `DispatchOutbox.recover_abandoned()` on a slower schedule
   than the drain. Without it, a worker killed holding its final attempt leaves an
   operation `PENDING` that no drain will ever pick up again — see above. The drain loop
   alone is not sufficient to guarantee every admitted operation reaches a terminal
   state.

Defaults worth knowing before running one: `DEFAULT_CLAIM_SECONDS = 60` (the lease a
worker holds), `DEFAULT_MAX_ATTEMPTS = 10` (after which the operation becomes `UNKNOWN`
and its row is **kept**, undelivered, with its reason, so an operator can see that
accepted work was never delivered).

A row that `recover_abandoned` has settled carries `abandoned_at`, which excludes it from
future claims: without that, a clock passing its lease would hand out work whose
operation is already terminal.

### Rollback and compatibility

`SCHEMA_VERSION = 1`. There is no earlier version to roll back to, so the only rollback
is removal: `await downgrade(connection, target=0)` drops all three tables.

The columns added for replay and claim ownership (`request_payload`, `job_id`,
`attempt_id`, `claim_generation`, `abandoned_at`) are part of **v1**, not a v2 migration,
because v1 has never been applied to any database — there is no deployed schema for a
migration to move. A `NOT NULL` column added to a table with live rows needs a backfill;
added to a table that has never existed, it needs nothing. Anyone who applied an earlier
build of v1 must `downgrade(connection, target=0)` and re-apply.

**It is destructive** — it discards admitted operations and undelivered dispatch rows.
Drain the outbox first, or accept that accepted-but-undelivered work is lost.

Compatibility is checked in **both** directions, and the newer-schema direction is the
one usually got wrong. Rolling code back while leaving a newer schema in place means old
code writing rows that violate an invariant the new schema added but the old code does
not know to maintain. A refusal to start is a visible outage; silently writing bad rows
is not. So when the schema is ahead of the code, `check_schema_version` refuses and says
*roll the schema back before the code*.

A stored version of `0` is unconstructible (`CHECK (version >= 1)`), because
`current_version()` reports `0` for "not installed" and the two states need opposite
responses: install the fresh one, refuse to touch the corrupted one.

## Status

Written, linted and tested against real PostgreSQL 18.4 (128 tests). **Not composed into
any running service, and no database has been migrated** — this is implementation and
offline verification only. Live release remains behind the named Wave 6 live gate; the
`operation_facade` port's live verifier is the Wave 6 operations evaluator (#5540).
