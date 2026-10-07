# Durable admission, dispatch and reconciliation contract

**Design contract for review approval and implementation handoff.** This is the
normative detail for D1/D2/D3 and review findings R1–R3. Runtime behavior remains
unimplemented and unqualified. Approval is recorded against an exact commit in
PR #7039 and epic #7030; a branch name is not an approval record.

R2 uses conservative containment, not a financial exception: saved trusted
receipts recover exactly once; genuinely lost observations remain unknown and
block affected spend. No operator write-off, guessed charge or automatic release
is part of this design. The reviewer corrects the overly broad assistant-authored
AC-02 explicitly below; this does not claim the requester approved a financial
risk exception.

## Verified starting point

- [Admission](../../../modules/gateway/src/budget/enforcement_service.py#L1089)
  confirms quotes before Redis reservation; ordinary reservation faults can
  degrade to the settled-ledger check, unlike strict policy reservations.
- [Reservation keys](../../../modules/gateway/src/budget/reservations.py#L251)
  include period boundaries; ordinary entries expire. Redis cannot be the durable
  authority for new unresolved-usage safety guarantees.
- [Accounting gaps](../../../modules/gateway/src/budget/enforcement_settings.py#L31)
  currently gate global/flow enforcement, not arbitrary tenant/user/person scopes.
- [Person resolution](../../../modules/gateway/src/budget/enforcement_service.py#L1632)
  and the [person ledger](../../../modules/gateway/src/budget/person_ledger.py#L205)
  fuse identities and sum cross-organization spend. Person resolution failure is
  currently contained/skipped; the proposed active authority cannot use that
  fallback to bypass an unresolved person hold.
- [Settlement](../../../modules/gateway/src/budget/settlement.py#L16) already
  claims `(org_id, request_id)` and debits atomically. Reuse that business receipt,
  not a competing charge ledger. The [tracker](../../../modules/gateway/lambda/budget-usage-tracker/handler.py#L367)
  and [legacy budget writer](../../../modules/gateway/src/budget/service.py#L288)
  must participate in the new lock order before activation.
- [Quote confirmation](../../../modules/gateway/src/orchestration/provider_quotes.py#L709)
  awaits an adapter revision read. Today's adapters use process-local pricing;
  this function alone is not a no-wait dispatch fence. The
  [pricing cache](../../../modules/gateway/src/budget/pricing_v2_reader.py#L165)
  is not a globally instantaneous generation view.

All schema, interfaces, state names and settings below are **proposed additions**.
Source baseline and current-main differences are in [the audit](audit.md).

## R1: durable scoped admission authority

### Authority and scope keys

Select **PostgreSQL as the admission authority**, with short transactions in the
request pool; retain S3 as the independent, acknowledged final-usage journal and
the existing SQL settlement receipt as the charge authority. S3 listing, Redis,
worker memory and recovery lag never decide that new spend is safe. Every active
gateway model route must acquire its durable hold before a provider call,
including non-streaming calls sharing these budgets. Counting-token/model-list
routes that cannot incur inference spend do not create a metering attempt.

Scope keys are versioned canonical tuples, not client strings or delimiter joins:

| Scope kind | Canonical binding and effect |
|---|---|
| Tenant hierarchy | `(tenant_id, entity_type, entity_id)` for authenticated user/service agent, attributed root user, team, department and organization, following existing settlement allocation. Unknown organization exposure blocks everyone sharing that organization, not unrelated organizations. |
| Person | `(person, fused_anchor)` without a tenant prefix, using the existing registered anchor resolver, not a guessed GitHub login. Direct human and attributed human-rooted agent work share this scope across organizations. Service-only principals remain non-persons. |
| Run/chain/accepted policy | Tenant plus verified run, chain and flow-policy identity/revision from the server binding. Keep these distinct from hierarchy spend to avoid counting the same charge twice in one denominator. |

The lock/gap identity is **period-independent**. Attempt-scope rows separately
capture applicable daily/weekly/monthly or policy allocation periods. Outstanding
bounds carry into current admission headroom until disposition; rollover does not
erase them. Settlement still charges historical periods using captured trusted
timestamps, never the recovery date. Resolve the whole attribution graph even
when a scope has no configured monetary cap: unknown-accounting safety must not
be evaded by deleting a cap, moving team or switching enforcement to observe/off.
Those switches continue to govern numeric cap enforcement, not this separate
safety gate. This is an explicit proposed compatibility change requiring review.

Person alias/link changes must lock old and new canonical scope keys and preserve
their outstanding bindings. Persist alias-to-scope relations while any attempt
remains open; admission unions those aliases rather than forgetting old exposure.
Changing current membership never rewrites historical settlement attribution.
Identity fusion or a governing-scope change that cannot be resolved safely refuses
the affected admission; it does not create a fresh empty scope. No tenant-facing
response reveals another organization's spend, membership or blocking request.

### Additive schema

Use new migrations after the actual implementation branch's Alembic head, not a
guessed revision number. SQL examples here are logical contracts, not runnable DDL.
New tenant-bearing columns use `tenant_id`; adapt to legacy `org_id` explicitly.

| Proposed table | Keys, fields and constraints |
|---|---|
| `metering_scope_gates` | PK canonical `scope_key`; scope kind, nullable tenant only for cross-org person scope, activation `legacy/draining/active`, `revision`, policy/binding revision, readiness state. Rows are stable mutexes for short SQL transactions, not leases over provider execution. No global row lock on ordinary admissions. |
| `metering_attempts` | PK `(tenant_id, request_id)`; verified owner, immutable attribution/quote/price snapshot and hashes, request digest, bounded amount (`NUMERIC`, nonnegative; never float), state, revision, producer epoch, preparation lease deadline, dispatch deadline, completion deadline, exact S3 keys, receipt version/hash if verified, reconciliation status/timestamps. Unique server attempt identity; immutable-field conflicts quarantine rather than overwrite. |
| `metering_attempt_scopes` | PK `(tenant_id, request_id, scope_key, allocation_period)`; FKs to attempt and gate, bound and captured allocation. Index by scope and attempt; partial/open-state lookup or equivalent measured plan must make active exposure lookup bounded. Rows survive period changes and have no TTL deletion. |
| `metering_scope_aliases` | PK `(alias_kind, stable_alias, scope_key)`; links registered person aliases to canonical gate keys, with identity revision. Old bindings cannot be deleted while open attempts reference them. Internal access only; not a cross-tenant lookup API. |
| `metering_reconciliation_events` | Append-only event ID, unique idempotency key plus request payload hash, tenant/request FK, actor/authority, expected/prior/new attempt revision, action/reason, protected evidence digest/reference, receipt hash if any, timestamp and resulting disposition. No prompt, credential, operator profile or fabricated token count. |

State constraints enforce required dispatch/receipt fields; all read/update APIs
bind tenant plus request and verify ownership. Do not cascade-delete accounting
evidence when a test identity is disabled. Existing `budget_usage`, person cap,
pricing and settlement tables remain authoritative for their present purposes.
No new S3 bucket, DynamoDB table or secret convention is required. An additive
nullable receipt hash/version reference may be stored on the attempt, not by
replacing the existing settlement receipt's key or semantics.

### Atomic transitions and request-path check

Proposed operations: `reserve_attempt`, `arm_dispatch`, `cancel_unsubmitted`,
`settle_receipt`, `mark_unknown`, `reconcile_unknown`. Each takes verified context,
the immutable attempt binding, expected revision/epoch and an absolute deadline;
returns a typed result, never a boolean that treats an unavailable check as allow.
No external I/O, Redis call, provider wait or client drain occurs inside SQL.

1. Resolve trusted context and a conservative cost bound before acquiring locks.
   Reuse quote adapters where supported; the ordinary character estimator is not
   a proven upper bound. Add explicit route adapters for translated Chat and any
   other priced path lacking a bound, binding both original and submitted payload
   digests. Unbounded features remain unavailable in active journal mode; do not
   silently fall back to legacy admission. This compatibility gate is tested per
   API/model before activation, especially Opus Chat and Astra Responses.
   Round reservation bounds upward to the ledger's six-decimal precision; retain
   the validated historical pricing decision for actual settlement. Reuse cannot
   turn an approximate estimate into a proven bound by renaming the field.
2. `reserve_attempt` inserts/locks all gate rows in canonical sorted order, then
   rechecks activation, authoritative policy/binding revisions and authorization
   validity. Read settled usage and open attempts **after** the locks under
   PostgreSQL READ COMMITTED. All participating settlement/cap-change writers
   acquire the same gates, so a waiter sees the preceding committed result.
   Deny any intersecting unknown/quarantined/overdue attempt; otherwise check
   `settled + open bounds + candidate bound <= applicable cap` independently
   for each enforced scope/period. Do not add SQL and Redis copies of the same
   hold. Insert the attempt plus every scope in the same commit, or insert none.
3. Commit `prepared` before writing the immutable S3 admission object. A confirmed
   admission-write failure permits cancellation only while the same epoch still
   proves the attempt unarmed. An ambiguous SQL commit means no dispatch: read
   back under the same key and hash within deadline, or fail closed and recover.
4. After acknowledged journal I/O and other preparation, `arm_dispatch` again
   locks scopes then attempt, checks other gaps, caps/policy/binding revisions and
   deadline, and CAS-transitions `prepared → dispatch_armed`. Its one-use permit
   contains attempt revision/epoch, quote identity and an absolute short expiry.
   Commit/release SQL before invoking the transport guard described under R3.
   An ambiguous arm commit cannot authorize sending; fail and leave recovery.
5. The transport consumes that permit at most once. Optional progress updates
   cannot remove the bound. `dispatch_armed` means **may have dispatched**, not
   proof of provider execution. Absence of a “sent” log is not evidence of no send.
   Client loss, process death or completion-deadline expiry makes it unknown.
   On every next admission, SQL checks overdue attempts synchronously even if
   the recovery worker is stopped; an expired row is a barrier, never zero.
   A live finalizer records a known accounting failure as `unknown` within its
   remaining cleanup deadline. If that write fails or the worker vanishes, the
   durable armed bound remains counted and becomes a blocking overdue row at its
   finite completion deadline. There is no claim of instantaneous remote crash
   detection; this bounded detection window never removes exposure.
6. `settle_receipt` fetches/verifies immutable S3 evidence **before** opening SQL.
   Lock gates → attempt → existing settlement receipt → aggregate rows in stable
   order. Verify attribution, original price, payload and trusted usage. Commit
   receipt/debits, one diagnostic usage row, and `settled`/hold removal atomically.
   No interval permits a hold to vanish before measured debit exists. A duplicate
   matches and does nothing; a conflict preserves the barrier and alerts. Redis
   reconciliation happens after commit and cannot roll back a durable charge.

Known final receipt arrival takes precedence over timeout/unknown classification
under the same attempt lock. `prepared → cancelled_unsubmitted` can occur only
by CAS before arming, or from an armed attempt with a live guard's proof that
the sole one-use permit was irreversibly cancelled before transport submission.
Recovery cannot manufacture that proof after a process kill. Prepared lease
takeover increments the epoch; a stale producer cannot arm or use a newer permit.
Once armed, recovery never reissues a dispatch permit or inference call.
An epoch update cannot remotely revoke a permit already held by a producer.
Accordingly no recovery or manual release may treat an armed attempt as unused
until its nonrenewable permit expires and the producer is fenced; expiry alone
still cannot prove it was not sent. A suspended old producer must run the local
deadline guard on resumption. Only prepared attempts support safe lease takeover.

Ordinary serving and settlement must share one lock-order implementation, including
the legacy budget writer, usage service, tracker, cap mutations and pricing
corrections affecting the checked denominator. Offline admin/migration operations
that cannot participate require admissions drained for affected scopes. Identity
link/membership changes use the alias/binding fence. These are #7032's schema and
admission changes coordinated with #7031, not assumptions about current behavior.

### Redis loss, rebuild and unavailable behavior

SQL is consulted on **every** active admission; no positive TTL cache can replace
the transaction. Loss of ordinary Redis reservations therefore does not lower the
SQL denominator. Rebuild a derived scope snapshot under its gate lock from settled
records plus **all** nonterminal attempts, including earlier periods and unknowns.
Return a SQL revision, release SQL, then publish Redis using revision/epoch CAS.
Re-read the SQL revision after publication; a mismatch discards/retries the cache
within bounds. Regardless of publication races, SQL remains the allow authority.

Strict policy accumulators retain their existing unavailable behavior: they may
not initialize to zero after a flush. Their rebuild must additionally include
existing policy/node reservations and settled policy evidence, not just gateway
attempts. If completeness cannot be proved, refuse that policy scope until trusted
reconstruction. Redis flush does not justify disabling strict checks or deleting
an accounting gap. This proposal does not claim existing flow meters already
implement the new durable per-attempt authority.

Unavailable SQL/identity/quote authority: sanitized 503 + bounded Retry-After,
zero provider calls, no fallback to a settled-only or stale cached allow. An
unknown accounting barrier uses a distinct safe `accounting_unresolved` category;
genuine numeric budget exhaustion retains 402, quota retains 429, authentication
denials retain 401/403. After headers use the existing protocol error path.
Scope contention denies only intersecting requests within the deadline; shared
organization/person denial is intended, not a cross-tenant data leak. A database
outage necessarily prevents all authority-dependent new work; do not promise
unrelated traffic survives a globally unavailable database. Existing in-flight
streams may persist final S3 receipts independently of SQL availability.

## R3: final dispatch and quote validity

Order the full path as follows; arrows crossing a SQL box occur after commit:

```text
verified context + bound → SQL prepared → acknowledged S3 admission
  → prepare credentials/client/route → SQL arm with policy revision check
  → bounded transport acquisition → final local quote/permit guard → send once
  → provider terminal + usage → acknowledged S3 final receipt → client terminal
  → SQL exactly-once settlement + hold removal → derived Redis reconciliation
```

Retain the existing early quote checks as cheap refusals. They are not the final
check after journal I/O. `arm_dispatch` is the authorization/policy linearization
point: it issues a short-lived, single-use permission under the current policy
revision. A later policy change blocks subsequent permits; it does not revoke a
provider invocation already handed off. No reusable “policy approved” cache.
Administrative reconciliation waits for outstanding permits to expire/be fenced.

Propose a **new dispatch guard**, shared by the Botocore and HTTPX provider paths.
Acquire executor/connection capacity, routing credentials, signing inputs and any
adapter I/O before its last check. Immediately before submitting provider request
bytes, synchronously compare the bound request/model/destination digest, quote
expiry, permit deadline/epoch and current immutable pricing revision. Coordinate
the local pricing snapshot publication and permit consumption with a short
in-process lock or serialized operation; release it on transport handoff, not
after the network response. There is no application await, executor queue, DB,
S3, Redis or retry delay between that check and submission. Socket/network wait
after submission is dispatched/ambiguous, even before response headers.

This is a required new transport contract, **not** a claim that an ordinary HTTPX
request hook or the existing awaited `confirm_quote_spendable` guarantees it.
Proposed adapter API: awaited `prepare_transport(binding, deadline)` returns an
owned, closed-on-cancellation handle; `dispatch_once(handle, permit, guard)`
performs the final guarded submission and returns a stream or typed
`proven_not_submitted` / `submission_ambiguous` failure. The handle cannot accept
another request or refresh a permit. Provider request bytes cannot be submitted
by preparation. This keeps quote ownership independent of provider SDK details
while making the one-use boundary and cancellation evidence testable.
#7033 must place the guard after transport pool waits (including retries/new
connections); #7032 owns the permit/quote contract. If the chosen SDK cannot
expose that boundary, use a bounded gateway-owned dispatch adapter or refuse the
mode; a check before `to_thread`/`client.send` is insufficient. Any adapter needing
remote quote validation must first return a verifiable bounded-validity token;
without that capability it cannot implement this no-wait guard and fails closed.

The pricing revision check preserves today's process-local publication semantics,
not a fictitious globally synchronous price read. An externally committed price
generation not yet published to a process is governed by existing pricing-reader
freshness/rollout rules; expired/unavailable snapshots refuse. Within a process,
generation publication before submission invalidates the permit; publication
after handoff affects later calls only. Test both orderings. Broader instantaneous
cross-fleet price revocation would require a separate approved protocol, not an
unacknowledged claim here.

Confirmed expiry/generation/policy mismatch before send: cancel the one-use permit,
then durably mark proven-not-dispatched and release **only that attempt**. If this
SQL release fails, retain its barrier for recovery using the durable cancellation
evidence; never release based on client error alone. Return 503/requote guidance;
no automatic new quote, reservation or inference retry inside the old attempt.
A future client retry is a new request with full authorization. Crash or uncertain
send after arming remains unknown; do not replay inference or release on timeout.

Candidate `BG_METERING_DISPATCH_PERMIT_SECONDS=1` is an upper limit, clipped to
the original quote expiry and whole admission deadline. Queue pressure that
consumes it produces a safe failure, not a refreshed permit. Use monotonic local
deadlines anchored conservatively to server/quote times; reject excessive clock
skew or clock rollback rather than extending validity. Historical receipt pricing
remains the admitted immutable snapshot; expiry after dispatch does not reprice
settled work. Invalid final usage or usage exceeding a validated bound raises an
integrity barrier for the affected scopes; retain evidence, never truncate cost.

## R2: conservative recovery boundary

The original assistant-authored #7032 AC-02 promised exact recovery after model
completion but before persistence. That promise is impossible if a process crash
destroys the sole usage observation and no provider recovery mechanism exists.
The design review corrects that acceptance wording rather than invent a recovery
API or present an admission intent as a measured receipt. Its ID remains AC-02.

**Corrected AC-02:** After a crash, replay any durable trusted usage receipt and settle exactly once at the business-record level, without duplicate or cross-tenant charges. If provider usage was lost before durable receipt storage, retain the durable attempt as unknown and block affected spend until trusted evidence resolves it; do not invent usage, treat it as zero, release it by administrative exception, or replay inference.

This resolves the design with the strictest operational outcome: availability
may remain impaired indefinitely for the affected scopes when evidence cannot be
recovered. A future write-off, debt forgiveness or release of unknown exposure
requires a separate product decision and reviewed design; it is excluded here,
not hidden behind a disabled flag. Local fault tests can prove containment but
cannot label the lost usage exactly recovered. Qualifying live campaigns still
fail on unknown usage, incomplete streams or unreconciled accounting.

### Proposed authorized reconciliation interface

Add an admin-only API, schema/tests owned by #7032, not an inference or tenant
self-service escape hatch: `POST /admin/metering/attempts/{request_id}/reconcile`.
Require a current authenticated **human platform administrator**, existing
`BUDGET_UPDATE` permission, explicit target tenant and protected incident authority.
Reuse [human admin controls](../../../modules/gateway/src/budget/enforcement_routes.py#L53)
and [person-cap authority](../../../modules/gateway/src/budget/person_cap_routes.py#L39):
an org admin cannot clear a cross-organization person barrier. No worker/service
credential, ordinary caller or metric exporter may use this administrative API.
Automated recovery applies verified receipts under its existing scoped authority;
it gains no manual-release capability.

Strict request fields: `tenant_id`, `expected_revision`, `idempotency_key`,
`action`, `reason`, `evidence_ref`, `evidence_sha256`; `receipt_key/version/hash`
only for a trusted-receipt action. Evidence locations are private configuration,
validated server-side, never arbitrary outbound URLs. No user-supplied charge,
token count, price or reconstructed transcript is accepted as measured usage.

| Action | Required evidence and durable result |
|---|---|
| `apply_trusted_receipt` | Gateway-signed/authorized producer record in the exact owned S3 key/version, matching attempt/owner/quote and validated provider usage. Run ordinary exactly-once settlement; no second charging path. New external evidence sources need a reviewed verifier, not an operator's assertion that a log is trusted. |
| `confirm_not_dispatched` | Persisted single-use guard cancellation proof with epoch and request digest, or a never-armed prepared attempt fenced by CAS. No receipt/debit; disposition `cancelled_unsubmitted`. Missing logs, age, no content, EOF and provider connection errors are not proof. |

Only the two actions above exist. Reject any request for `close_unknown_by_exception`,
manual hold release or estimated settlement as an unsupported action without
mutation. Admin status and an incident note are not evidence of provider usage or
proof that a request was never dispatched. Removing a proven-unsubmitted attempt
cannot bypass other authorization, quotas, caps or unknown attempts. Strict
policy scopes remain unavailable if complete trusted reconstruction is impossible.

Return disposition, new revision, whether spend remains blocked, and whether an
exact receipt exists; no cross-tenant details. Duplicate identical idempotency
keys return the same result, conflicting keys or stale revisions return 409,
invalid evidence 422, insufficient authority 403, dependency failure 503 with no
state change. Do not release any other attempt. SQL commits the audit event and
hold disposition atomically; optional external audit export cannot substitute
for that record. Read/modify authority and protected evidence access are audited.

### Late evidence and forbidden release conditions

Unknown is not a charge. Preserve it until evidence-backed disposition, without
TTL, period rollover, recovery retry limit, account disablement, Redis flush,
enforcement-off, a new quote or operator retry clearing it. Quarantine has the
same admission effect as unknown. Retain the canonical attempt and audit records;
fixture cleanup and transcript expiration cannot delete this evidence.

A trusted late receipt follows the ordinary exactly-once settlement path from
unknown. Debit original historical scope/period/price once, atomically replace
the hold with the measured charge, and preserve the recovery audit. Receipt and
reconciliation races serialize on scope/attempt locks; stale revisions return
409, identical replay remains idempotent, conflicting evidence quarantines the
attempt. Never manufacture a current-period charge or estimated token count.

A trusted receipt arriving for an attempt previously marked proven-unsubmitted
contradicts its cancellation evidence: quarantine and reblock the affected scopes,
retain both records and alert the authorized operator. Do not silently choose one
history or double-debit. Resolution requires verification of the evidence, not a
financial exception endpoint. Cover this case in F07 alongside ordinary late
receipts and duplicate/stale administrative requests.

## Costs, compatibility and recovery

The authority adds at least two request-pool transactions per successful dispatch
(prepare and arm), one accounting transaction for settlement, possible bounded
release/reconciliation operations, and two S3 acknowledgements. It deliberately
trades some availability/latency for correctness; S3 only decouples **post-stream**
settlement from SQL, not admission from SQL. Person/hierarchy locks can serialize
busy shared identities. Measure lock wait/hold time, transactions/attempt, rows
examined, WAL/storage growth, alias lookup cost and admission/first-content tails.
No new engine/pool beyond D1; recovery/diagnostics count against the total database
budget and must have bounded concurrency. Do not raise replicas to hide this cost.

Candidate whole admission deadline 5s, each authority transaction ≤2s including
checkout/rollback, lock wait ≤500ms, S3 prepare/terminal handoffs ≤3s each under
their parent deadlines; stages share remaining time, not fresh timeout budgets.
Preparation lease ≤5s; armed completion deadline equals the existing bounded
provider operation plus ≤3s handoff (candidate ≤303s). Requests requiring longer
lifetimes need an explicit finite configuration before admission. Recovery scans
SQL indexed open attempts every minute, ≤100/page, ≤1,000 and ≤30s/invocation,
using CAS leases/epochs; it fetches exact known S3 keys outside transactions.
Overdue barriers are checked synchronously by admissions regardless of scan lag.
Bound per-scope open work as well (candidate `BG_METERING_MAX_OPEN_PER_SCOPE=1000`);
admission refuses additional work rather than letting queries/backlogs grow
without limit. Never evict an old hold to satisfy that bound. A due-attempt index
and fair scope scheduling must prevent one unresolved scope starving others.

Deploy additive migrations and all compatible writers/readers before activation.
Default journal flag remains off. Transition a **closed set of shared scopes**
`legacy → draining → active`: stop their new admissions, drain old provider and
settlement work, reconcile known receipts, refuse activation with unresolved
legacy exposure, and record a verified authority baseline. Do not backfill a
missing receipt as zero. A canary identity sharing a person/org scope with legacy
traffic is not isolated; either include all those writers or do not activate.
The environment flag may prevent activation of new cohorts, but cannot turn off
an already-active SQL gate. Gate-aware code checks durable activation before any
legacy bypass; incompatible configuration refuses serving those scopes. Verify
this explicitly when disabling the flag during rollback.
New scopes initialize under the same lock protocol, not from a cache miss.
Old gateway/Lambda versions that do not acquire the gates cannot coexist with
active-authority spend. Verify versions and fencing before reopening traffic.

Rollback stops affected new spend, keeps capable recovery/readers and all tables,
aliases, receipts and audit records, then drains/reconciles. Old binaries cannot
resume serving active scopes merely because the flag is off. No destructive
downgrade while rows exist; forward-compatible inactive tables may remain. The
named operator owns migration, activation, rollback and reconciliation receipts;
this PR performs none. #7031 owns connection/lock containment, #7032 authority and
schema, #7033 transport guard, #7035 signals, #7036 fault automation and #7034
capacity measurement. See [the required fault matrix](campaigns.md#accounting-and-dispatch-fault-matrix).
