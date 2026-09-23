# Harness Jobs — durable operation store, dispatch outbox and admission gate

Issues [#5525](https://github.com/aws-e/adp/issues/5525) (w6-02) and
[#5526](https://github.com/aws-e/adp/issues/5526) (w6-03), EPIC #4910, Wave 6.
Implements the `operation_facade` port declared in
[#5524](https://github.com/aws-e/adp/issues/5524)'s registry
(`superplane_contracts.integration`, owner `harness_jobs`).

An accepted operation must not be lost, must not be run twice, and must not be
visible to another tenant. This package is where those three properties are made true,
once, for every consumer that opens a long-running operation.

**A stored operation is not permission to spend.** `OperationStore.admit()` answers
"is this well-formed, unique and tenant-scoped" and deliberately never "is this
allowed". `admission.admit_operation()` is the gate that answers the second question —
a current approval bound to an immutable plan and a spend envelope, reserved and
confirmed against the domain's ledger, consumed exactly once. A request handler should
reach the gate, not the store.

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
| `harness_jobs/approval.py` | `ApprovalRecord`, `ApprovalBinding`, `SpendEnvelope`, `ApprovalResult`, `evaluate_approval`, `APPROVAL_PERMISSION` |
| `harness_jobs/admission.py` | `admit_operation`, `BudgetLedger`, `CreationFence`, `Reservation`, `ReservationState`, `IntentStage`, `AdmissionIntent`, `DispatchEvidence`, `cancel_before_dispatch`, `retain_for_uncertain_dispatch`, `list_interrupted_admissions`, `reconcile_interrupted_admissions`, `read_consumption`, `read_consumption_privileged` |
| `harness_jobs/inventory.py` | `InventoryAuthority`: `publish_report`, `enumerate_resources`, `record_provider_enumeration`, `seal_allocation`, `read_inventory`, `assess_cleanup`; `AllocationResource`, `VerifiedInventory`, `CleanupAssessment`, `ResourceObservation`, `ResourcePresence`, `ReleaseState`, `CostExposure`, `report_digest`, `allocation_id_for`, `MAX_INVENTORY_RESOURCES` |
| `harness_jobs/allocation.py` | What `execution` and `inventory` must agree about, below both: `lock_allocation`, `sealed_revision`, `allocation_id_for`, `approved_allocation`, `CallEffect`, `call_effect`, `may_create`, `creating_calls_unaccounted_for`, `bounded_text`, `MAX_ALLOCATION_ID_LENGTH`. Exists because `inventory` imports `execution`, so `execution` cannot import `inventory` — and `execution` is where refusing still prevents spend |

### The admission sequence (#5526)

    (1) approval     evaluate_approval — current authority, binding, envelope
         |
   (1b) intent       harness_admission_intent row, COMMITTED before (2)
         |           → the obligation is enumerable even if this process dies
         |
    (2) reserve      domain ledger hook, idempotent on (job_id, attempt_id)
         |           → budget held. NOT a spend. Compensatable.
    (3) confirm      domain ledger hook, same key
         |
    +----------- ONE transaction, in the store ---------------------------+
    | (4a) operation record + (4b) outbox row + (4c) consumption row      |
    +--------------------------------------------------------------------+
         |           commit is the durability point.
    (5) deliver      outbox → executor. At-least-once, duplicate-safe.

Steps (1)–(3) cross a process boundary and sit **outside** the transaction, per #5524
§3.1. A transaction held open across a ledger call would make the store's throughput a
function of the ledger's latency and would hold locks on the admission tables while a
hung ledger timed out. What makes that safe is idempotency plus the fact that a
reservation is *not a spend*: holding one too long costs headroom, releasing one too
early costs correctness.

The job and attempt identity the ledger is keyed on is **derived from the approval id**
(`derive_operation_identity`), and `admit_operation` accepts no `operation_id` /
`job_id` / `attempt_id` parameters. A caller who could choose the key could present one
approval twice and be granted a second hold, because from the ledger's side those would
be two different attempts. Deriving it makes "no budget renewal through retries"
structural rather than checked.

### The domain ledger callback interface

`BudgetLedger` and `CreationFence` are `Protocol`s and this package implements **neither**.
Domain accounting stays Superplane-owned: `accounting.py` states that a second place
computing cost is a second answer that can disagree with the real one, so this module
holds the *ordering* and the *compensation* and calls out for every quantity. It performs
no arithmetic on the envelope's amounts, and `tests/test_admission_bypass.py` asserts
that by walking the AST rather than by reading the code.

The implementer owes four methods. `reserve` and `confirm` must be **idempotent on
`(job_id, attempt_id)`**, and a repeat under the same key with a *changed envelope* must
raise `BudgetDenied` rather than be honoured — silently honouring it is how a retry
becomes a budget increase.

`BudgetUnavailable` (a `RuntimeError`) and `BudgetDenied` (a `PermissionError`) are
deliberately different types: unanswered is not refused. The distinction decides
compensation — a *denied* confirm releases, an *unavailable* one retains.

| Failure | The one safe answer |
|---|---|
| (2) reserve refused | Nothing was reserved; nothing to compensate |
| (2) ok, (3) confirm lost | **Retain.** Retry confirm under the same key |
| (3) ok, (4) commit lost | *Establish* nothing was dispatched, then release |
| Cancellation before dispatch | **Fence creation, then release.** Never the reverse |
| Dispatch uncertain | **Retain** until provider reconciliation. Never release |

The last one looks wrong and is not: releasing would let one envelope fund a second
operation while the first may be running, and `CostExposure.NONE` is unreachable without
provider-established absence. `cancel_before_dispatch` returns a `ReservationState`
rather than `None` precisely so a *failed* fence is not treatable as a successful one —
it retains.

### "Nothing was dispatched" has to be established, not assumed

Two rows of that table — the lost commit and the cancellation — turn on knowing that no
executor has seen the work. The presence of an outbox row is *not* that knowledge, and
neither is its absence in general: a delivered row is deleted, so "no row" can mean
never-enqueued or already-delivered-and-cleaned-up.

`DispatchEvidence` is the classification, computed by reading the row under `FOR UPDATE`:

| Evidence | What the row says | Answer |
|---|---|---|
| `NEVER_QUEUED` | no row at this `operation_id` | safe to release |
| `DEFINITELY_PENDING` | `attempts = 0`, no lease, not delivered, not abandoned | fence, withdraw the row, release |
| `CLAIMED_OR_DELIVERED` | anything else | **retain** |

`attempts = 0` rather than `claimed_until IS NULL` is the pending predicate, because
`_record_failure` clears the lease while leaving `attempts` standing — a lease-only test
would read a failed attempt as never-queued. `claim` increments `attempts` in the *same*
`UPDATE` as the lease, so the row is its own witness: a row no claim has ever taken was
never handed to a worker.

The classification and the fence run in **one transaction**, and that is safe to hold
briefly because `claim` uses `SKIP LOCKED` — a concurrent drain skips the locked row
rather than blocking on it. The ledger call stays outside, because a release is not
undone by a rollback, and released-hold-with-reverted-withdrawal is the one combination
that funds work nobody is accounting for.

### Recovering an interrupted admission

Steps (2) and (3) cross a process boundary, so a process can die between issuing a
reserve and hearing its reply. The hold exists; nothing in the admission tables mentions
it. Step **(1b)** is what makes that recoverable: a committed `harness_admission_intent`
row naming the derived `(operation_id, job_id, attempt_id)`, the tenant, and a **copy of
the approved envelope** — copied because recovery matters precisely when the approval has
expired or been revoked and the approval store can no longer answer.

`IntentStage` records how far the sequence is *known* to have got. It may lag reality and
must never run ahead of it: each value is written only after the corresponding reply
arrives, so `reserved` implies a hold exists while `intended` implies nothing either way.
The sweep resolves that ambiguity by re-asking the ledger under the derived key, which is
idempotent — a false "maybe" costs one redundant call, a false "no" would cost a leaked
hold.

```python
outstanding = await list_interrupted_admissions(connection, limit=50)
report = await reconcile_interrupted_admissions(connection, ledger=ledger)
# → ReconciliationReport(scanned, released, retained, unresolved)
```

The sweep **admits nothing and widens no budget**. It only releases, retains, or leaves
the row unresolved for the next pass — and it retains, never releases, when a dispatch
row exists. `unresolved` is the number an operator watches: a row that keeps failing to
settle stays in the set, by design.

Admission and recovery require an exclusively held PostgreSQL connection outside a
caller transaction. They serialize each approval with a session advisory lock across
reserve, confirm and commit. Recovery skips live writers, rereads stale enumeration
results under ownership, and resolves an intent only after acknowledged compensation.
A failed release remains enumerable. Connection loss releases ownership and prevents
that writer from committing. A recovered-and-released admission needs a new approval;
replaying an already consumed approval never renews its reservation.

### Reading a consumption record

`read_consumption(connection, principal, *, approval_id=...)` takes the **resolved
principal** and scopes the SQL by `org_id`/`workspace_id`. An approval id is a bearer-ish
string, and returning reservation, operation and plan metadata to anyone holding one
makes it a cross-tenant read primitive. Absent and another-tenant's are the same answer
(`None`) on purpose.

Global recovery reads are a separate, explicitly named function —
`read_consumption_privileged` — so a caller that wants to bypass tenancy has to say so at
the call site rather than by omitting an argument.

### Bounded request sizes

Finite by construction, enforced in `OperationRequest.__post_init__`:

| Limit | Value |
|-------|-------|
| `MAX_IDEMPOTENCY_KEY_LENGTH` | 200 |
| `MAX_PARAMETER_COUNT` | 50 |
| `MAX_PARAMETER_KEY_LENGTH` | 100 |
| `MAX_PARAMETER_VALUE_LENGTH` | 2000 |
| `MAX_TOTAL_PARAMETER_BYTES` | 16384 |

## Allocation inventory and cleanup authority (#5529)

`inventory.py` answers two questions the domain cannot answer for itself: **what does
this allocation actually own**, and **may its unused budget be released**. It holds no
balance and computes no amount — it returns a `BudgetDisposition` per resource for the
domain ledger to apply, for the reason the ledger-callback section above gives.

Seven rules, each of which is the whole point of the module:

1. **A caller never hands in a digest.** `publish_report(...)` takes observations,
   canonicalises them itself, stores the payload and derives `report_digest` from what it
   stored. `read_inventory(..., report_digest=...)` recomputes the digest from the stored
   payload and compares with `hmac.compare_digest`. Echoing the caller's digest back would
   make "attested" mean "asserted", which is exactly what an attestation is for. The
   attestation's *identity* is the full binding — digest plus operation, workspace,
   attempt, executor and fence — supplied from the resolved grant, never from the request.
   The digest alone would be wrong both ways: identical canonical observations are routine
   (a successor re-querying an unchanged provider produces the same bytes), so legitimate
   publications were refused and cleanup became permanently unavailable; and two
   publications of the same bytes under different grants both saw no row, one insert was
   discarded, and both callers were told they had succeeded. The insert is now an upsert
   whose returned row is verified to be the caller's own. The binding also names the
   **sealed revision** the report was taken against — see rule 4.
2. **Membership is a table, not a provider reference.** `provider_ref` on
   `harness_provider_call_intent` is one reference per *call*, and a single call routinely
   creates several independently billable things. `enumerate_resources(...)` records each
   one as a row with a durable provider handle, and the set's identities hash to an
   inventory `revision` so a grown allocation is visibly a different inventory. Its
   `operation_id` is **provenance with no foreign key**: membership must outlive the
   operation row, and a cascade there let routine operation housekeeping silently shrink an
   inventory whose whole contract is that it only grows.
3. **Membership cannot prove its own completeness.** Checking each succeeded call's own
   handle only confirms the executor wrote down what it was already reporting; it cannot
   see one call creating several billable resources with only the headline one recorded — a
   cluster enumerated without its disk, which released everything while the disk kept
   billing. `record_provider_enumeration(..., provider=..., provider_references=..., generation=...)`
   records what the **provider** says it holds and is refused when the provider names a
   handle membership does not. The stored listing is a digest compared against the handles
   enumerated *now*, so a listing that has since been outgrown reads as a mismatch, not as
   a proof; and it cannot be recorded before creation is confirmed complete. A listing is
   evidence **for the authority that took it**: the proof must name the operation,
   attempt, holder and fence relying on it, because comparing only provider and digest
   let a predecessor's listing satisfy a successor's fence — after recovery advanced the
   fence, the new holder could seal and release on a provider query it never made. A
   successor must re-enumerate before it can seal. A report is additionally bound to the
   **listings that existed when it was taken**: each listing row carries a database-advanced
   `generation`, a report stores an `enumeration_binding` digest over the
   `(provider, generation)` pairs current at publication, and verification recomputes it.
   Without that, a listing taken *later* retroactively validated an earlier report — the
   executor could observe "absent" and publish, then re-list, and the fresh listing would
   satisfy the completeness proof for an observation that predated it. The binding makes a
   report verifiable only against the evidence it was actually taken against.
4. **A release-authorizing snapshot seals the allocation, and a report is taken after the
   seal.** `read_inventory` must release its transaction before the domain acts, so while
   membership stayed writable a release decided from "only the cluster, and it is gone"
   could be acted on after a disk was added. `seal_allocation(...)` is an explicit write
   under the live fence; `enumerate_resources` reads the seal inside the same transaction,
   so additions after it are **refused and audited**. Only a sealed allocation is ever
   `complete`, and the seal names the revision it covers — recomputed on every read, so a
   row inserted behind the package's back makes the inventory incomplete rather than
   sealed. The seal is also what dates a report: publishing only needed a live lease, so
   an executor could query the provider *before* creating anything, publish the truthful
   "absent", then create and seal, and present that earlier report to release budget over
   a resource that exists. `publish_report` therefore refuses to publish into an unsealed
   allocation, stores the sealed revision it was taken against, and verification requires
   it to equal the revision sealed now — so membership growth or a reseal retires every
   attestation taken before it.
5. **Writes that change what an allocation contains are serialized per allocation, not
   per operation.** Two separately approved operations in one tenant can name the same
   allocation, and then their lease locks are two different locks: one checks for a seal,
   the other reads membership and seals over it, a release is authorized, and the first
   commits a late resource into an allocation that can never be sealed again. Membership
   writes, the enumeration proof and the seal all take a transaction-scoped advisory lock
   on `(org_id, workspace_id, allocation_id)` **before** the lease lock, through one
   shared helper so the order cannot diverge into a deadlock. Either the seal wins and
   the growth is refused, or the growth wins and the seal is refused; never both.
6. **Sealing withdraws the authority to CREATE, not only the ability to record.** Rule 5
   orders database writes, and that is all the seal used to do. It did not order the
   **provider call**. Two approved operations naming one allocation: after A sealed, B
   could still record an intent and invoke the provider, and B's membership write was then
   refused — so the resource existed and billed, appeared in no inventory, and A's sealed
   membership plus its ABSENT report authorized releasing the budget for it. Every check
   passed. Two rules close it, both under rule 5's allocation lock:
   **(a)** a creating call may not be recorded or dispatched into a sealed allocation —
   enforced in `execution` (`record_intent`, `OperationExecutor._record`, and again in
   `_refuse_sealed_before_dispatch` immediately before the provider hook, the last moment
   at which refusing still costs nothing); **(b)** an allocation may not be sealed while a
   creating call recorded against it is unaccounted for — enforced in
   `seal_allocation` and re-derived by `_completeness`. Neither suffices alone: (a) leaves
   the window between a committed intent and its in-flight provider call, and (b) closes it
   using the durable intent row rather than a lock held across provider I/O, which would
   block sealing on unrelated provider latency and is lost on a crash. Whether a call can
   create is read from the approved plan's `operation_kind` (`allocation.CallEffect`),
   never from a worker argument, and an **unrecognized verb counts as creating** — "we do
   not know" must answer yes to "could this start costing money". Teardown and query calls
   are always permitted, because a sealed allocation that can no longer be torn down bills
   forever, which is the same loss from the other direction.
7. **Incomplete is not empty, and unknown is not absent.** Completeness is re-derived on
   every read from six independent sources — the resource ceiling,
   `confirmed_plan_progress`, every recorded provider call being resolved *and* represented
   in membership, a current provider listing per provider, the seal over exactly this
   revision, and no creating call from *any* operation against the allocation being
   unaccounted for. None of them is a stored boolean, so a flag cannot outlive the state it
   described — in particular the seal row is never trusted, so a seal written around rule 6
   by a restored backup or a future writer makes the inventory read incomplete instead of
   releasing money. `assess_cleanup` derives the expected set from stored membership,
   never from the observations handed to it, so an observation set that simply omits a
   resource cannot release it. A resource the provider could not be consulted about stays
   `UNKNOWN`, keeps `CostExposure.UNRESOLVED` and `ReleaseState.UNRESOLVED`, and retains
   budget.
   `may_return_reservation_unused` is true only when every expected resource was
   independently established absent.

The executor's full sequence is therefore: `enumerate_resources` (as resources are
created) → finish the plan → `record_provider_enumeration` per provider →
`seal_allocation` → `publish_report`. Only then can `read_inventory`/`assess_cleanup`
report `complete`. **The order is the contract, not a convention:** membership writes and
the enumeration proof are refused once the seal is taken, and `publish_report` is refused
until it is — so the sequence above is the only one the authority accepts. Creating
provider calls belong before the seal for the same reason and are refused after it
(rule 6); teardown and query calls remain available afterwards, which is how a sealed
allocation is actually cleaned up.

`read_inventory` returns a single `None` for every failure — absent, another tenant's,
unattested, fenced out. A caller that could tell those apart would have a cross-tenant
probe and a fence oracle.

Refusals are audited **outside** the aborted transaction. A refusal audit written inside
it rolls back with the refusal, so the fence doing its job would leave no trace — the same
ordering `execution.py` uses.

Composition owes an `authenticate` verifier that turns a caller's credential into an
`ExecutionGrant`. There is no default, for the same reason `BudgetLedger` has none: a
permissive default makes cleanup authority satisfiable by omission. Locally submitted
observations cannot manufacture it.

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
lane, because a run of nothing but skips is green and a lane reporting green while
asserting none of these guarantees is worse than no lane.

One test module, `tests/test_admission_bypass.py`, deliberately carries **no**
module-level database mark. A check that the admission gate cannot be bypassed must not
be the check that is silent on a developer machine.

CI: [`.github/workflows/harness-jobs-ci.yml`](../../../.github/workflows/harness-jobs-ci.yml),
job **`Harness jobs store tests`**. It lints, runs the suite against a real server, and
then asserts the run was not vacuous — a floor on the executed count, because the exit
code alone cannot distinguish "everything passed" from "the database half never ran".
The floor must stay **above the offline-only count**: a run where every database test
skips executes exactly the offline half, so a floor at or below that number is satisfied
by the very failure it exists to catch. Raise it when the offline half grows.

The lane also asserts that **nothing skips at all**, which catches what a floor cannot.
`HARNESS_JOBS_REQUIRE_POSTGRES=1` turns a missing database into a failure and `pydantic`
is in the `dev` extra so the HITL contract is importable, so this lane has removed every
skip reason the suite has — a remaining skip means an upstream became unimportable and a
drift guard quietly stopped guarding. That is why the assertion exists: on this lane's
first green run the two agreement tests that read the four HITL result values off
`contracts/hitl-ticket/v1` skipped for want of pydantic, and 257 executed sailed over a
floor of 200 while the check that the duplicated approval vocabulary had not drifted ran
nowhere. A widened permissive set upstream would have merged under a green tick.

## Deployment

This package ships no service and no Terraform. Deploying it means, for whoever composes
it (#5535):

1. **Install the schema.** `await apply(connection)` — idempotent, so a retried
   install is safe. It creates `harness_operations`, `harness_dispatch_outbox`,
   `harness_jobs_schema_version`, (v2) `harness_approval_consumption`, (v3)
   `harness_admission_intent` and (v7) `harness_provider_report`,
   `harness_allocation_resource`, `harness_allocation_enumeration` and
   `harness_allocation_seal`.
2. **Check compatibility at startup.** `await check_schema_version(connection)`, or
   `OperationStore.ensure_compatible()`. It refuses to run against a schema older *or*
   newer than `SCHEMA_VERSION`, and says which direction to move.
3. **Supply the seams.** A `connect` callable returning an async context manager
   yielding a connection, and a `PrincipalResolver` that resolves identity from verified
   request context. Both belong to the composition root; neither can be defaulted here.
   For the admission gate, also a `BudgetLedger` — and, for cancellation, a
   `CreationFence`. There is no default implementation of either *on purpose*: a
   fallback ledger would be a budget authority this package must not hold, and a
   permissive default would make the gate satisfiable by omission. #5524 §3.6 warns
   specifically against wiring these to `enforce_workspace_creation_quota` or
   `CostReconciler._suspend_workspace`, which would put admission authority in the
   domain app; and this package does **not** bind to
   `modules/gateway/src/budget/reservations.py`, whose key is `request_id` rather than
   `(job_id, attempt_id)` and whose ledger is a per-request token budget rather than a
   provisioning envelope. An adapter over it would be a separate, named decision.
   For `InventoryAuthority`, also an `authenticate` verifier returning an
   `ExecutionGrant` — undefaultable for the same reason, since a permissive default would
   let a caller's own observations establish cleanup authority over an allocation.
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
7. **Run the reconciliation sweep.** `reconcile_interrupted_admissions(connection,
   ledger=...)` on a slower schedule still. This is the budget-side counterpart to (6):
   without it, a process killed between issuing a reserve and hearing its reply leaves a
   hold that no admission record mentions and no drain can see. The sweep admits nothing;
   it releases what is provably unspent, retains what is uncertain, and reports
   `unresolved` for what it could not settle. `unresolved` staying non-zero across passes
   is the signal that a hold needs a human.

Defaults worth knowing before running one: `DEFAULT_CLAIM_SECONDS = 60` (the lease a
worker holds), `DEFAULT_MAX_ATTEMPTS = 10` (after which the operation becomes `UNKNOWN`
and its row is **kept**, undelivered, with its reason, so an operator can see that
accepted work was never delivered).

A row that `recover_abandoned` has settled carries `abandoned_at`, which excludes it from
future claims: without that, a clock passing its lease would hand out work whose
operation is already terminal.

### Rollback and compatibility

`SCHEMA_VERSION = 7`. **v2 adds `harness_approval_consumption`** (#5526) — the durable
record that one approval was spent on one operation, with `approval_id` as the primary
key and `operation_id` `NOT NULL UNIQUE`. Those two constraints are the single-use rule:
the first stops a second admission under one approval, the second stops a second approval
paying for one operation. The foreign key is `ON DELETE RESTRICT`, deliberately not
`CASCADE` — a cascade would mean deleting an operation silently frees its approval for
reuse, turning a row deletion into a budget grant.

Added as version 2 rather than folded into v1 because v2 needs no backfill (a new table,
not a new `NOT NULL` column) and because "no database has been applied" is not verifiable
from here — #5535 and #5538 may already have applied v1, and for those databases `apply()`
must run the v2 statements alone. `apply()` and `downgrade()` are version-generic, so
`await apply(connection)` reaches v2 from either 0 or 1.

**v3 adds `harness_admission_intent`** (#5526) — the durable pre-ledger intent row that
makes an interrupted reserve recoverable, keyed `approval_id` PRIMARY KEY with
`operation_id NOT NULL UNIQUE`, and carrying a copy of the approved envelope so
reconciliation still works after the approval has expired. It has **no foreign key**, on
purpose: the row exists before the operation and consumption rows do, which is the entire
ordering problem it was added to escape. Its only index is partial on
`stage <> 'resolved'`, because the interesting set is permanently tiny and a sweep that is
expensive gets scheduled rarely — and a reconciliation that runs rarely is a hold that
sits for hours.

**v7 adds the four inventory tables** (#5529): `harness_provider_report`,
`harness_allocation_resource`, `harness_allocation_enumeration` and
`harness_allocation_seal`. They arrive together because a release is authorized from all
four read as one answer — membership without a provider listing cannot show it is whole,
and membership that can still grow cannot be released against at all.

An attestation is keyed by its full grant — `(report_digest, operation_id, org_id,
workspace_id, attempt_id, executor_id, fence_token)` — so identical canonical
observations from two grants are two rows and a predecessor's attestation is simply not
found for a successor. The digest alone as the key made a legitimate republication
permanently refusable and let two concurrent publications of the same bytes both report
success while one insert was discarded. It also carries `sealed_revision text NOT NULL`,
the revision sealed when it was published: an attestation that cannot say which
membership it was taken over cannot be dated, and an undated one authorized releasing a
resource created after it. `NOT NULL` is the point — a nullable column would make
"published before the seal existed" storable again. Membership is keyed
`(org_id, workspace_id, allocation_id, resource_id)`, which makes one identity per
resource per allocation structural rather than checked; the listing is one row per
`(allocation, provider)` — carrying the operation, attempt, holder and fence that took
it, so a proof is only current for the authority relying on it — and the seal one row per
allocation. All carry
`fence_token bigint NOT NULL CHECK (fence_token >= 1)`, so a row written by something that
never held a grant is unstorable rather than merely suspicious.

`operation_id` on membership and on the attestation is **provenance with no foreign key**.
It carried `ON DELETE CASCADE`, which meant routine operation housekeeping made the
database shrink an inventory whose entire contract is that it only grows — leaving a
still-billing resource unenumerated and the remainder reading as a complete allocation.

The listing additionally carries `generation bigint`, advanced by the database on each
replacement, and an attestation carries `enumeration_binding text NOT NULL` — a digest over
the `(provider, generation)` pairs current when the report was published. Without it a
listing taken *later* retroactively validated an earlier report, so an "absent" observation
published before the provider was re-queried could still satisfy the completeness proof.
The generation is the database's, not a caller's, because a value the publisher chose would
let it name a listing that suited it.

**v7 also adds `allocation_id text` to `harness_provider_call_intent`** — a v4 table, so
this is the one v7 change that is not a new table. It is what makes the creation fence
(rule 6) enforceable: `creating_calls_unaccounted_for` finds *another operation's* in-flight
creating calls against an allocation, and without the column that check silently matches
nothing. Denormalized deliberately — the alternative is decoding every operation's payload
on the release path while holding the allocation lock — and copied from the digest-bound
approved plan, never from a worker argument. Nullable, because "this call names no
allocation" is a real answer for the many operations that are not allocation-bound; its
index is partial on `allocation_id IS NOT NULL` for the same reason the sweep index is
partial, and because that query runs under the lock every membership write and every seal
is queued behind.

Before publishing schema version 7, the migration installs a compatibility trigger and
backfills existing provider calls from each operation's digest-bound request. The trigger
recomputes the same length-prefixed SHA-256 digest as admission. Malformed allocation IDs
and corrupt payloads refuse activation; only an omitted allocation ID means no allocation.
Old workers that omit the new column are bound automatically. Creating inserts take the
same allocation lock and refuse a sealed or quarantined allocation. Settlements advance
the allocation epoch, invalidating earlier listings and reports. Unresolved creating calls
block sealing until their outcomes and resulting membership have been accounted for.
Interrupted migrations can be retried; no manual worker-drain step establishes correctness.
This authors the upgrade only; the separately authorized installer applies it.

`await downgrade(connection, target=6)` drops all four tables and that column.

> **Rolling back to v6 discards the only record of which resources an allocation owns.**
> After it runs, `assess_cleanup` has no expected set to reconcile against, so an
> allocation holding live compute or retained storage is indistinguishable from an empty
> one and its budget looks releasable. Published attestations go with it, so a domain
> holding a digest gets `None` and stays unresolved — the safe direction, but permanently.
> The completeness proofs go too, and an inventory is not reconstructible from the
> remaining tables. Dropping `harness_provider_call_intent.allocation_id` additionally
> removes the allocation binding until v7 is reapplied. Reapplying v7 restores call
> bindings from surviving approved operations, but cannot restore deleted membership,
> listings or reports. **Reconcile outstanding
> allocations to `RELEASED` or escalate their `UNRESOLVED` resources to a human first,
> then roll back.**

`await downgrade(connection, target=2)` drops only the intent table;
`await downgrade(connection, target=1)` also drops the consumption table; `target=0`
removes the store entirely.

> **Rolling back to v2 makes outstanding holds unreclaimable.** Nothing becomes
> *reusable* — the hazard runs the other way to v1's. The ledger keeps the budget, and the
> only record naming the `(job_id, attempt_id)` needed to ask about it is gone; a
> reservation is released by someone deciding to release it, so it does not come back on
> its own. **Run `reconcile_interrupted_admissions` until it reports nothing outstanding,
> then roll back.**

> **Rolling back to v1 discards every record of which approvals have been consumed.**
> After it runs, an approval that already admitted an operation and already reserved
> budget is indistinguishable from an unused one, so replaying it admits a second
> operation and reserves a second time against an envelope a human approved once.
> **Revoke or expire outstanding approvals first, then roll back.**

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

Written, linted and tested against real PostgreSQL (723 tests, including the admission
sequence, one-time consumption under genuine concurrency, dispatch-evidence
classification, interrupted-reserve reconciliation, tenant-scoped reads, forged digests
and stale fences against the inventory authority, the omitted-child and
release-then-growth cases, a report taken before creation, a predecessor's listing under
a successor's fence, a listing taken *after* a report and refused as its attestation,
two operations contending for one allocation, a sealed allocation refusing a second
operation's provider creation while still permitting its teardown,
operation-retirement durability, and schema v2/v3/v7).
`pgserver` publishes no wheel for Python 3.13, so obtaining a server means a 3.12
interpreter; where none is available the database half runs only in the CI lane — which is
why `HARNESS_JOBS_REQUIRE_POSTGRES=1` and the vacuity floor exist rather than trust in a
local green run. A suite that skips its database half reports 163 passed and 560
*skipped*, and a skipped test is not a passing one. #5529 adds 10 to the offline half
(the inventory contract-agreement guard) and the rest to the database half, which leaves
CI's vacuity floor of 390 above today's offline 163 and below today's total 723 — check
that arithmetic when adding offline tests, because a floor that has drifted below the
offline count is satisfied by the very empty run it exists to catch.

Measuring that offline number means making the database genuinely unobtainable, not
merely unconfigured: `conftest.py` finds `pgserver`/`embedded_postgres` by `find_spec`,
so unsetting `HARNESS_JOBS_TEST_POSTGRES_URL` on a machine where either package is
installed still runs the full suite. An earlier revision of this paragraph claimed an
offline half of 332 for a total of 678; both halves were measured with a server still
reachable, and the offline figure — the one the floor is compared against — was more
than double the truth. Overstating it is the dangerous direction, since it is the number
that makes a floor look safely above the vacuous run.

That lane earned its keep on the first run: it caught a schema assertion that compared
against the wrong type (asyncpg decodes PostgreSQL's `"char"` as bytes) and proved a
refusal branch in `admission.py` unreachable, since deriving `operation_id` from the
approval id makes the store's constraint fire first. The branch was removed rather than
left as dead code that looks load-bearing.

**Not composed into any running service, and no database has been migrated** — this is
implementation and offline verification only. Live release remains behind the named Wave 6
live gate; the `operation_facade` port's live verifier is the Wave 6 operations evaluator
(#5540).


### Worker execution and recovery boundaries

Trusted service composition retains `OperationExecutor` exclusively in the service
process. Workers receive `ExecutionClient(socket_path, scoped_run_token)` from
`harness_jobs.execution_rpc`. The client contains no connection factory, provider hook,
lease object or callable service implementation. Every message crosses a Unix socket
and is authenticated by the service's required verifier, which returns an
`ExecutionGrant` binding the verified principal to one operation/attempt/holder/fence.
The server rejects arbitrary methods and identity overrides. It executes the owned
transaction, committed intent, trusted provider hook, observation and audit itself.
Repeated keys cannot repeat provider mutation. Transport failures remain recoverable.
The server caps request size, simultaneous handlers and request/provider timeouts;
exception contents and authentication tokens are never returned.

Run service and worker as separate UIDs/containers. Only the socket and scoped run
credential are shared; database/provider credentials and the service's composition
configuration stay in the service container. A same-process object or shared service
environment is unsupported. The service socket is owner-only by default, or group-only
for the configured worker GID. The authenticated facade additionally exposes
`execution_status`, `cancel_operation` and `report_execution`. It resolves permission
and tenant from verified request context. Cancellation derives its audit actor from
that principal. `report_execution` retains an authenticated refusal/audit surface;
worker-supplied terminal reports cannot establish provider truth. Foreign and missing
operations have identical results.

Recovery takes a finite 60-second claim without increasing execution attempts. It
holds that claim across provider observation and locks/verifies it again before every
write. Expired runtime is recoverable even when lease expiry is in the future. A lost
recovery claim cannot settle a provider call, close a successor, or report completion.
Retries release the claim for real reacquisition. Already observed calls participate
in recovery outcomes. `SweepResult.call_dispositions` retains every call's SETTLE,
RELEASE or RETAIN decision; `budget_disposition` is `mixed` when these differ. The
owning domain applies those decisions to its ledger. Successful spend is settled,
never reported as released. Low-level database functions are internal service APIs;
workers use only the authenticated RPC client.


Schema version 5 adds persisted reconciliation attempts/backoff and a durable
`cleanup_required` flag. Apply the additive migration before deploying this executor;
the v4-to-v5 migration preserves existing call records. Schema 6 adds the durable
execution-attempt ceiling: the first grant establishes it, and acquisition/recovery
use it even after restart with different defaults. Existing v5 leases migrate to the
prior default of five without resetting attempts. Reconcile pending provider
calls and resolve cleanup before rolling back these fields.

Transport failures and UNKNOWN hook results remain INTENDED. Recovery commits each
observation attempt before I/O, using three attempts by default and persisted 30/60/120
second backoff. Restarts cannot bypass that delay or reset the attempt budget. A
confirmed observation settles the original key without repeating the mutation;
exhaustion escalates to UNRESOLVED with budget retained. Orphan recovery uses the same
retry accounting. `RecoveryReport.deferred` distinguishes scheduled observations from
completed outcomes or lost-claim skips.

Both recovery sweeps stop awaiting each observation after 30 seconds by default. Set
`observation_timeout_seconds` to a finite value greater than zero and at most 30
seconds (below the 60-second recovery claim). A timeout
follows the UNKNOWN retry/exhaustion path: budget is retained, the orphan advisory
lock is released, and later operations can proceed. Cancellation of the sweep itself
still propagates; recovery always rechecks ownership before writing a result.

Observer hooks perform asynchronous read-only provider I/O and must not use the
recovery database connection or block the event-loop thread. At the deadline the
hook task is cancelled without awaiting its cancellation cleanup. A process-wide
registry retains at most 50 outstanding observer tasks and prevents duplicate
observations of an intent within the same loop until its previous task exits.
Late results never update durable state; late exceptions are consumed. Occupied
slots or a still-running observer follow the same UNKNOWN path without starting
another hook. Tasks that refuse cancellation consume their slot until they exit or
the worker process is terminated; recovery still releases its locks and progresses.

Cancellation is rechecked after provider I/O and atomically at terminal settlement.
The recorded provider effect is retained; `CancellationPending` carries that call and
its budget disposition to trusted composition. SUCCEEDED settlement is refused when
cancellation is pending. CANCELLED settlement requires confirmed absence; existing or
uncertain effects set `cleanup_required` and recovery records UNKNOWN pending cleanup.
The domain's authorized cleanup flow consumes the stored provider identity; cancellation
does not silently authorize extra provider mutations. Recovery claim, observation,
backoff, retry, lost-claim refusal and settlement events are durably audited with the
recovery holder and fence token, without serializing provider exception contents.


Terminal settlement locks the operation and lease while checking every recorded call.
SUCCEEDED, FAILED, and CANCELLED refuse INTENDED/UNRESOLVED calls, RETAIN dispositions,
or pending cleanup. UNKNOWN preserves unresolved effects. CANCELLED additionally
requires every call to establish absence. A successful explicit `settle` is the trusted
workflow driver's completion attestation; recorded calls alone are never a complete
step plan. Recovery therefore reports UNKNOWN after a crash with successful recorded
steps, preserving their SETTLE dispositions and stating that workflow completion is
unconfirmed. The composition layer must resolve/resume that incomplete workflow.

If cancellation is observed after intent commit but before provider invocation, the
executor atomically records ABSENT/RELEASE plus a cancellation audit, then cancels the
operation only if all its other effects are resolved and absent. `CancellationPending`
carries the recorded result to the caller, including RELEASE for this no-call case.
Cancellation after dispatch retains the existing reconciliation/cleanup behavior.

The trusted `acquire` service API owns a transaction (or participates in its caller's
outer transaction). Every grant and its tenant/holder/attempt/token audit commit or roll
back together. Refusals for existing operations are audited with their stored tenant,
the requesting holder/attempt and refusal reason; no other holder or token is exposed.
Unknown operations produce no operation audit. Rolling back an outer transaction rolls
back its audit as well; normal standalone refusals commit before raising.


Run the trusted process through its maintained entry point:

```sh
python -m harness_jobs.execution_rpc --socket /run/execution/service.sock \
  --worker-gid 2000 --composition your_trusted_service:execution_composition
```

The trusted async context-manager factory must yield `ExecutionRPCServer` with its
connection factory, provider adapter and current run-credential verifier. There is no
anonymous/default verifier or credential fallback. #5535 composes the reviewed vault
adapter and lease registry; #5538 packages that trusted process and isolated worker.
The runtime does not grant itself scopes or alter the AI-DLC execution policy.
Workers use `await client.request("execute_step", step_id="...")`, `status`, `cancel`,
`cancel_requested`, `renew` or `release`; argument
schemas are the trusted runtime methods, with `duration_seconds` for renewal. Responses
contain only JSON values; cancellation carries its durable call and budget disposition
in `ExecutionRPCError.response`. A lost response must be reconciled, never blindly
retried as a fresh provider mutation.

Verify the actual separate-process boundary without any cloud credential or spend:

```sh
HARNESS_JOBS_REQUIRE_POSTGRES=1 PYTHONPATH=modules/harness/jobs \
  python -m pytest modules/harness/jobs/tests/test_execution_service.py -q
```

Apply schema 6 before startup. Drain workers and recovery before rollback to schema 5;
that downgrade removes persisted retry policy and requires policy re-establishment
before any old runtime resumes. No migration is executed by merely importing the API.


### Approval-bound provider steps and safe recovery

The admitted request must include `parameters.execution_steps`, a JSON string containing
an ordered list of 1–16 descriptors, each with exactly `step_id`, `provider`,
`operation_kind`, and `target`. Step IDs must be unique. The approval covers the entire
request digest, including these descriptors; production composition must construct and
authorize them before admission. Missing or malformed plans refuse execution. The service
reads and verifies the stored payload on every step request. Workers can name only a step
ID, and cannot substitute a provider, target, kind or idempotency key. Preceding steps must
have trusted successful observations before a later step is dispatched. Only the service
records outcomes and completes the operation after every admitted step succeeds; RPC raw
intent/observation/settlement methods and facade worker terminal reports are refused.

Provider idempotency keys derive from the tenant, operation, approved plan digest and
step ID, and remain stable across process/attempt changes. Trusted provider adapters
must honor that key, including transport failures. The service holds an operation-specific
PostgreSQL advisory lock across provider I/O; recovery cannot claim the operation while
the hook is in flight. Intent and audit transactions still commit before the call. An
expired holder cannot be taken over by ordinary acquisition: recovery must first establish
whether retry is possible. Only an attempt with no provider-call history, no cancellation
and no cleanup obligation can release a live lease. Historical effects also prevent direct
acquisition even if an older implementation already cleared the holder.

`cancel_operation` completes an unheld cancellation using the existing atomic creation
fence and outbox withdrawal path. Untouched queue entries are withdrawn and budget is
released after commit; previously claimed work or historical effects retain budget.
Ledger failures leave terminal/fenced database evidence and the original reservation
identity for an idempotent cancellation retry. Held operations remain the responsibility
of their executor or recovery sweep; worker release cannot strand their cancellation.


### Provider listing cutoff and contradiction recovery (#5529)

Before calling the provider's allocation-list API, trusted composition calls
`attempt = await authority.begin_provider_enumeration(connection, lease, provider=...)`.
After the API returns it passes that exact `attempt` to
`record_provider_enumeration(connection, lease, provider=..., provider_references=...,
attempt=attempt)`. The attempt binds a unique durable query ID, provider and creation
cutoff. Never begin after querying the provider. If the provider call returns an error,
call `fail_provider_enumeration(connection, lease, attempt=attempt)`; failed listings
block reports until a fresh successful listing. Do not mark a still-running call failed.
An interrupted process leaves an in-progress row. A successor can open a fresh listing
only after that row's recorded execution grant expires, closes or is superseded; the
old token can never complete the replacement. No manual database step is needed.
Mutating or unknown provider intent insertion, settlement, reconciliation or deletion
advances a durable allocation generation, including reused or absent handles.
Proven read-only calls preserve that generation.
A changed generation requires a new provider query; seals and reports compare it too.
The listing write owns its transaction so a refused contradiction cannot be rolled back
by a caller transaction. This API is for trusted adapters, not isolated workers.

A provider listing that discovers an unrecorded handle commits the discovered identity
and quarantines the allocation before returning refusal. Every older report is revoked
across operations. Creating calls remain fenced, while teardown and observation remain
available. Recovery records all discovered resources, obtains fresh listings, reseals
the complete inventory and publishes new observations. The quarantine is cleared only
by successful resealing; an empty later listing cannot erase a recorded discovery.
These records are part of the pending v7 upgrade/downgrade. No live migration is
performed by tests or code-only merge.


### Provider report freshness

Compose `InventoryAuthority(query_provider=...)` with a trusted fresh-read adapter.
Its `(lease, resources, query_id)` arguments identify the current operation, durable
provider handles and a new query nonce. Use the query ID for a fresh read through
`OperationExecutor`; do not return cached observations. After recording the current
provider listing and sealing membership, call `receipt = await authority.observe_report(
connection, lease)`, then `await authority.publish_report(connection, lease,
observations=receipt)`. Submit the returned receipt's observations to the domain API,
including each `observation_id`. Raw dictionaries cannot publish attestations.

The authority captures the listing and seal before the query, stamps the provider
response and checks the same bindings again at publication. A concurrent listing or
mutation invalidates the receipt. Republishing one receipt is idempotent while its
bindings remain current; it never overwrites them. A genuinely new query gets a new
observation ID and digest, so old absence evidence stays invalid after proof replacement.
Attestations created without a query receipt are refused by the release reader.

The epoch trigger and executor share the exact provider/action allowlist in `effects.py`.
Only a reviewed read-only action under its bound provider preserves the epoch. An arbitrary name containing
"read", "describe" or "delete" is not authority: ambiguous actions remain
creation-capable, including `ec2:PromoteReadReplica` and `read_write_volume`.
Known teardown actions remain available after sealing; unknown teardown spellings
require a reviewed allowlist entry. Mutating and unknown calls advance the epoch. This permits a
post-listing provider query without invalidating its own prerequisite listing while
retaining the fences for reused/null-handle creation and contradictory discoveries.

The pending v7 migration also creates `harness_provider_query`, whose allocation-scoped
key `(org_id, workspace_id, allocation_id)` stores the latest query nonce. The nonce commits before provider I/O. A newer
query therefore invalidates older reports immediately, including when the newer
query fails or has not yet published. Publication and release both require the
current query nonce as well as the original seal/listing binding. This prevents
falling back to old absence evidence after an unsuccessful refresh.

Freshness is shared by every operation on an allocation, matching membership,
sealing and the creation epoch. A query under operation B invalidates operation A's
older report for that allocation, even if B's query fails. The report itself still
binds its authenticated operation, attempt, executor and fence. Independent tenants,
workspaces and allocations do not supersede each other's observations.

Beginning a provider listing durably advances the allocation's query nonce and records
an in-progress attempt in `harness_provider_listing`. While any allocation listing is
in progress or failed, report observation, publication, sealing and completeness
refuse. A per-resource read cannot supersede a list-all request which might discover
an omitted child. Only the exact begin token can complete its listing; failed or stale
responses cannot complete another attempt. Both durable tables belong to the pending,
retry-safe v7 migration; no live migration runs here.
