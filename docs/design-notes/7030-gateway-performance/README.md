# Gateway reliability and measured capacity — coordinated design

**Design and implementation checkpoints; approval is recorded at an exact PR commit.**
This proposal covers [epic #7030](https://github.com/aws-e/adp/issues/7030) and its
six existing stories. It is a design delivery, not a defect fix, deployment,
capacity certification, or permission to run billed tests. In particular, **200
concurrent sessions are not yet supported by the supplied evidence**.

## Understanding and approach

Developers and parallel agents should receive complete model streams without
database stalls, misleading success responses, or lost usage charges. Operators
should be able to reproduce failures, restore test fixtures after a crash, and
state a capacity limit tied to a specific workload and measured latency target.
Today the supplied campaign shows failures below or at the target, while the
current implementation mixes request serving with accounting and depends on
deployment-specific scaling settings.

The approach is to instrument and reproduce the failures first, shorten and
isolate database transactions, make accounting recovery independent of the
client connection, enforce truthful streaming completion, and only then measure
capacity. Existing tenant authorization, budget reservations, pricing decisions,
settlement receipts, deployment tooling, and the Locust harness remain the
foundation. A larger pool or replica ceiling is not a substitute for a fix.

## Review package and evidence boundary

- [Repository audit and source evidence](audit.md): complete tracked-file ledger,
  migration graph, entry-point ownership, exclusions, and remaining gaps.
- [Draft story designs and implementation backlog](stories.md): reuse #7031–#7036,
  retain every acceptance ID, identify dependencies and shared-file ownership.
- [Campaign and cleanup contract](campaigns.md): proposed finite scenarios,
  metrics, stop/success rules, recovery, and epic acceptance mapping.
- [Durable admission and dispatch contract](accounting.md): additive scoped SQL
  authority, final quote guard and proposed authorized reconciliation.
- [Review finding-to-change mapping](review-changes.md): R1–R3 disposition and
  the conservative accounting resolution and explicit acceptance correction.
- [Validation record](validation.md): checks actually performed for this proposal.

Source baseline: `fc5fe6f21100df5d49c29f4c3a882907ff8e659e`.
Campaign reference: [#6994](https://github.com/aws-e/adp/pull/6994),
`f51d030c77e80a9cf80971c4a42940da2622ccd2`. The relevant proxy, budget,
rate-limit, pool, usage, chat-logging, database, profile, and performance-harness
trees have no diff between these revisions; other gateway code has advanced.
Issue bodies and the [requester's amendment](https://github.com/aws-e/adp/issues/7030#issuecomment-6003885514)
were read on 2026-10-05. Campaign numbers below are **supplied sanitized evidence**,
not measurements made by this design review. No live target, SQL graph, provider
exception trace, image digest, or private campaign artifact was accessed.
The [consolidated review](https://github.com/aws-e/adp/pull/7039#pullrequestreview-5422134420)
and [revision request](https://github.com/aws-e/adp/issues/7030#issuecomment-6005911251)
were checked on 2026-10-05/06. The final review resolves R2 by retaining unknown
exposure with no financial exception and explicitly corrects the overly broad
assistant-authored AC-02; see the finding-to-change mapping. No requester approval
of a write-off or manual release is claimed. R1 corrects the initial assumption
that an S3 intent and later scan alone could protect subsequent admissions.

## Findings: observation versus hypothesis

| Finding | Evidence and consequence |
|---|---|
| Astra failed its target plateau | Supplied #7030/#7031: 1,962/2,100 successes at 200; admission stopped around 201 seconds, before five minutes. A two-minute 150 stage passed 1,371/1,371. Neither a sustained limit nor developer count follows. |
| Opus failed earlier | Supplied #7033: 113/122 overall, nine incomplete HTTP-200 streams at 60 configured sessions, maximum sampled in flight 57. Later 20/20 direct checks from each of two locations at concurrency ten do not reproduce the gateway path. |
| Lock and pool failure occurred, but the blocker is unknown | Supplied #7031 records long lock waits and an idle transaction; no original blocking SQL. Shared user, organization-wide settlement rows, auth/session ownership, and cancellation are investigation candidates, not diagnosed causes. |
| Gateway finalization can extend HTTP lifetime | Supplied #7032 has a 165.45s client lifetime versus 38.54s upstream log. Source [S03](audit.md#source-evidence) awaits usage work after that log in Responses. This does not attribute every extra second to SQL. |
| Existing recovery is not an acknowledged durable handoff | [S04–S07](audit.md#source-evidence): SQL receipts deduplicate debits; S3 events can recover them, but optional chat logging schedules fire-and-forget publication. SQL failure followed by process loss before S3 persistence can lose measured usage. |
| EOF is not necessarily provider completion | [S08](audit.md#source-evidence): the Chat translator emits a final marker on normal iterator exhaustion as well as on `message_stop`. A silent upstream EOF can therefore be misrepresented; this is a source-level regression risk, not the proven cause of the nine campaign failures. |
| Capacity and observability are configuration-dependent | Supplied #7034/#7035: two-to-six scale-out took about two to three minutes; metric publication was denied. [S10–S13](audit.md#source-evidence) show distinct default/profile manifests and multiple metric mechanisms. The failing exporter/namespace and actual role require correlation. |

Successful-only p95 first-content latency rose 2.94s → 9.72s and HTTP lifetime
19.92s → 114.51s. These percentiles omit failures. One shared identity, repeated
synthetic prompts, one request per session, and approximately 99.85% cached
successful Astra long-prompt input do not represent 200 distinct developers and
their agents. No OOM was observed; restart intent/unreadiness is not a recorded
restart. Preserve these distinctions in every future report.

## Proposed decisions and explicit gates

The review approval record identifies the accepted exact revision. Implementation
and live qualification remain separate stages; the coordinator names owners before
dispatch. Candidate settings are experiments, not measured production defaults.

| ID | Recommendation | Status / prerequisite |
|---|---|---|
| D1 — transaction containment | One short transaction per database operation; no open SQL session while waiting on model output, Redis/network calls, or client drain. Separate request-serving and accounting pool capacity, without increasing the combined connection cap. | Proposed. #7031 must identify the actual blocker before selecting table/index/lock changes. |
| D2 — accounting durability | New scoped SQL authority atomically reserves every admitted attempt; acknowledged S3 admission/final receipts support recovery independently of client delivery. Reuse the existing charge receipt. Revalidate quote at the transport dispatch boundary after journal I/O. | Revised proposal: [authority/schema and reconciliation](accounting.md). Saved trusted receipts recover exactly once; pre-receipt lost usage remains unknown and blocks affected spend. No administrative release or financial exception is included. AC-02 is explicitly corrected in the review record. |
| D3 — streaming reliability | Require a real provider terminal; preserve provider failure/cancellation semantics; no automatic inference replay after dispatch with uncertain outcome or after content delivery. | Proposed. #7033 still needs the failing transport path reproduced or externally attributed. |
| D4 — capacity policy | Retain the two-pod floor and existing opt-in CPU profile as a comparison baseline, not a certified policy. Integrate explicit profile selection into both deploy paths and derive ceilings from the total connection budget. | Proposed. Final targets, resource settings and latency SLO require measurement and owner sign-off before qualification. |
| D5 — diagnostics | Add low-cardinality lifecycle/pool/exporter signals and an operator-scoped blocking-graph capture; distinguish EMF log metrics from direct CloudWatch calls. | Proposed. Identify the denied writer and role before granting its namespace. |
| D6 — campaign controller | Extend the existing Locust parser with a single authoritative budget/controller and durable fixture recovery. Protected manual live runs only; deterministic no-cloud tests on PRs. | Proposed. Named target operator, token/cost caps and latency SLO are mandatory inputs. |

### Epic readiness checklist

- [x] Inspect parent amendment, six stories, #6994, and current code/deploy paths.
- [x] Prepare coordinated proposal, complete inventory, and draft story backlog.
- [x] Define all epic and child acceptance mappings without changing IDs.
- [x] Revise R1/R3 contracts and prepare R2 reconciliation for re-review.
- [x] Resolve R2 conservatively: retain unknown exposure; exclude financial exceptions.
- [ ] Record design review approval against the exact final commit in PR/epic.
- [ ] Coordinator names executor and live-validation owner for each story.
- [ ] After approval, incorporate decisions into each existing issue's Design
  section, with a permalink to the approved revision and approval record.
- [ ] Mark each story ready only after its prerequisites below are evidenced.
- [ ] Implement, review, deploy under separate authority, and complete live ACs.

The PR review and epic carry the immutable approval record and story readiness
checklist. Approval accepts the design and its explicitly corrected AC-02, not
runtime delivery, measured capacity or permission to dispatch/deploy. Unknown
accounting remains a blocking runtime condition; no manual risk acceptance is
part of this work. Implementation readiness follows each checkpoint's evidence.

## Shared architecture and invariants

```text
verified caller → approval/quota checks → SQL scoped hold → S3 admission intent
    → SQL dispatch arm → final quote/permit guard → provider → content stream
    → trusted usage + provider terminal
    → bounded durable receipt acknowledgement → client success terminal → EOF
                              │
                              └→ tracker / bounded recovery scan
                                  → SQL receipt + debit + usage row + hold removal
                                  → trusted reservation reconciliation
```

The diagram is the proposed D2 path, not current behavior. Failure before a
durable handoff must never be converted to a successful generation.

1. **Authority is immutable per attempt.** Derive tenant, user, root principal,
   membership, model permission, destination and reservation scopes from verified
   server context. Do not trust a client-provided tenant, account, request key, or
   metering payload. New envelope fields use `tenant_id`; adapt to existing SQL
   `org_id` only at that boundary. Personal anchor formats and organization
   identities are unchanged. No cross-tenant fallback on errors.
2. **Admission remains enforced per request.** The proposed active SQL authority
   checks all shared hierarchy/person/policy scopes on every admission and arms
   dispatch only after journal I/O. Ordinary Redis degradation cannot bypass it;
   strict policy checks still fail closed. Observe/off cap modes do not clear
   unknown-accounting barriers. This is a proposed behavior change, not a claim
   about today's fallback paths. No pending/unknown hold expires into permission.
3. **One generation, one accounting identity.** Server-generated request ID plus
   tenant is the business key. Provider attempts are separate observations under
   that key, with their own token/cost reservations. Clients may retry a failed
   request as new work; the gateway does not promise inference idempotency.
4. **Completion is protocol truth, not transport status.** Success requires content,
   a valid model terminal, no error/incomplete state, and a completed client read.
   Keep provider success, durable metering, and client delivery as separate states.
   Disconnected clients can still incur measured spend. A billed usage receipt is
   not evidence that an answer reached the client.
5. **Retries repeat storage work, not a partially delivered inference.** Durable
   writes and settlement use stable identities, bounded attempts and deadlines.
   A failed DB transaction rolls back before retry; an uncertain commit is read
   back by receipt before repeating any debit. Cancellation closes provider bodies
   and returns DB resources; shielding has a deadline, never infinite cleanup.
6. **Optional logs cannot own required accounting.** Transcript scrubbing/logging
   may be disabled without disabling usage receipts. No prompts, response text,
   bearer tokens or credential values are needed in a metering event. Diagnostic
   exporters cannot block the request; required durable handoff may fail closed.

## Database transactions and pool isolation — D1

Keep the current IAM password callback, TLS verification, pre-ping, recycle and
opt-in behavior ([S01](audit.md#source-evidence)). `reset_engine()` is not an
overload recovery mechanism: disposing/replacing pools while work is active can
multiply connection pressure. Preserve local SQLite test support, but use real
PostgreSQL for lock, timeout, cancellation and connection-cap claims.

Proposed API in `shared/database.py`: `get_session_factory(purpose="request")`,
with explicit `purpose="accounting"` for gateway finalizers. Existing call sites
remain source-compatible; split mode is an opt-in setting. Each transaction
must complete before returning a value; callers receive immutable context/data,
not session-bound ORM objects that trigger later lazy reads. Do not blanket-wrap
all middleware in one shared transaction. Existing SQL settlement receipt,
hierarchical debits, usage-row deduplication and the new durable hold disposition
remain atomic in the accounting transaction. Receipt replay mismatch is a
data-integrity error, not retryable. Admission transactions use the request pool;
all participating writers use the [same scope-first lock order](accounting.md#atomic-transitions-and-request-path-check).
The authority adds SQL work even though final receipts no longer wait for SQL;
#7031 must measure this cost and contention rather than assume isolation removes it.

Proposed private-test starting settings (not measured production defaults):

| Configuration | Proposal and rule |
|---|---|
| `BG_DB_POOL_ISOLATION_ENABLED` | New, default false. When enabled require bounded pooling; reject inconsistent configuration at startup. |
| Existing `BG_RDS_POOL_SIZE` / `BG_RDS_POOL_MAX_OVERFLOW` | In split mode request pool candidate 6 / 0; retain existing defaults when disabled. |
| `BG_ACCOUNTING_POOL_SIZE` / `BG_ACCOUNTING_POOL_MAX_OVERFLOW` | New, split-mode candidate 4 / 0, no third implicit engine. |
| Existing `BG_RDS_POOL_TIMEOUT_SECONDS` | Candidate 1 second for both pools; compare against current 10 seconds. |
| `BG_DB_LOCK_TIMEOUT_MS` / `BG_DB_STATEMENT_TIMEOUT_MS` | New, candidate 500 / 2000; apply transaction-local settings, not session-global state leaking across checkouts. |
| `BG_DB_IDLE_TRANSACTION_TIMEOUT_MS` | New, candidate 5000 on runtime connections, separate from migration/admin sessions. |
| Accounting overall operation deadline | Candidate 3 seconds including checkout, SQL and rollback; no nested timeout restarts extending that deadline. |

Validate positive finite settings and `lock < statement < idle` bounds; a driver
timeout must not interrupt required rollback and leave a checkout occupied.
If cleanup fails, invalidate that connection and measure replacement. Map
pre-stream dependency exhaustion to a sanitized 503 with bounded retry guidance,
not invalid-credential 401; keep actual denial 401/403 and existing quota 429.
After stream headers, use compatible stream errors/termination rather than
attempting to change HTTP status. #7031 owns exact error integration.

**Investigation checkpoint:** first capture a real blocker/waiter graph while the
wait exists: backend transaction IDs, transaction ages, wait class, application
operation tag, sanitized SQL fingerprint and caller identity class. Full SQL
text/bind values stay out of public output. Compare one shared user, distinct
users in one tenant, and distinct tenants; shared organization aggregate rows
can contend even with separate users. Reproduce cancellation both as a waiter
and as a blocker through the actual request/middleware path. This selects the
fix; it is not permission to guess an index or raise the pool.

## Stream finalization and durable recovery — D2

### Reuse and changes

Existing SQL `budget_settlement_receipts` has primary key `(org_id, request_id)`,
cost, tokens, owner, allocation hash and nullable trusted reservation scopes.
The current helper commits the receipt and all budget debits together with the
usage write. `usage_logs` has a scoped request index, **not a uniqueness constraint**.
The tracker currently bridges an existing usage row; it does not reconstruct a
missing diagnostic row. See [schema audit](audit.md#database-and-storage-state).

Recommend a mandatory usage-only journal in the **existing private chat-log S3
bucket**, independent of the optional transcript flag. This keeps final receipt
handoff independent of congested SQL settlement and reuses tracker/pricing
contracts. It does **not** make SQL-independent admission safe: the revised
[durable authority](accounting.md#r1-durable-scoped-admission-authority) adds five
tables for gates, attempts, scope allocations/aliases and reconciliation audit.
They are admission/recovery state, not a second charge ledger. Two bounded S3
writes and at least two short pre-dispatch SQL transactions add latency and
availability dependence; measure both against the predeclared SLO.

**Proposed interface:** `prepare_metering(verified_context, request_id, bound)`
first calls the scoped SQL `reserve_attempt`, then returns an immutable admission
identity only after S3 acknowledgement. It does not authorize provider dispatch;
`arm_dispatch` and the [final quote guard](accounting.md#r3-final-dispatch-and-quote-validity)
must succeed after all preparation waits.
`commit_metering(identity, final_envelope)` returns an acknowledged receipt or
a typed timeout/conflict/unavailable error. Both operate within monotonic absolute
deadlines, use bounded SDK retries, and never silently return success on a circuit
breaker skip. Do not call the current fire-and-forget writer as though it were
this interface. Use a dedicated bounded I/O executor so provider reads cannot
starve the write. Cancellation cannot kill a synchronous thread; its maximum
SDK call/retry lifetime and close behavior must fit the configured bound.

Keep the current transcript key
`<tenant>/<user>/<YYYY>/<MM>/<DD>/<request>.json` unchanged. Proposed journal keys
use a distinct `metering/v1/<YYYY>/<MM>/<DD>/<HH>/<shard>/<tenant>/<user>/`
prefix with immutable `<request>.admission` and `<request>.metering` objects.
This is an explicit new S3 namespace in the existing bucket: time-first discovery
supports bounded orphan audits without enumerating every tenant. Normal recovery
uses exact keys in the SQL attempt index, not listing completeness as authority.
The shard is a fixed hash partition
of the server request ID; initial partition count 16, versioned with the schema.
These JSON-encoded objects do **not** match today's `.json` notification;
explicitly add `.metering` delivery only after the dual-reader is deployed.
Validate the entire versioned key against the envelope. Do not overwrite optional
transcripts or change their privacy settings. Put with create-only semantics; on an ambiguous
acknowledgement, read the exact key/version and compare a canonical payload hash.
A matching duplicate succeeds; a different payload is quarantined. Bucket
versioning is not itself deduplication.

| Envelope | Required fields / validation |
|---|---|
| Admission v1 | Schema/kind, tenant/request/verified owner, request timestamp, model/API, verified attribution snapshot, exact SQL/legacy reservation scope bindings, admitted maximum charge, pricing/quote identity/expiry/generation, payload digests/hash, SQL attempt revision/epoch. No prompt or credentials. Write after SQL prepare and before SQL arming/provider dispatch; this object alone is not dispatch authority. |
| Final metering v1 | Admission identity/hash; provider outcome; usage-known flag; measured token/cache dimensions; captured routing/pricing decision and generation; verified settlement scopes; diagnostic usage-row fields, lifecycle durations; canonical payload hash. Validate against admission, not current membership or current price. |
| Recovery status | SQL prepared / dispatch armed / unknown or quarantined / settled / proven unsubmitted. Receipt durability and Redis reconciliation are separately recorded facts. CAS revisions fence stale writers; never rewrite a measured receipt to a new price. Financial exceptions are excluded. |

Exact attribution must include the current validated persona/run evidence without
loosening its all-or-none checks. No raw provider exception, response body or
public correlation identifier belongs in exported summaries. Retain provider
correlation privately only where permitted by the existing record contract.

Extend the existing tracker to accept old settlement-version-1 transcripts and
the new typed metering envelope. It validates key/owner/request and pricing,
locks/claims the same SQL receipt, updates the budget once, and inserts the
missing usage row under that receipt lock. Simultaneous gateway and tracker
writers must deduplicate the diagnostic row as well as the debit. In active
authority mode they first lock the captured SQL scope gates and attempt, then
commit hold disposition in the same transaction as the charge. The
[additive migration contract](accounting.md#additive-schema) is part of this
proposal, not deferred inventory or an implementation-time design choice.

Make the tracker the normal settlement writer in journal mode; optional old
transcript events remain idempotent compatibility inputs. Reconcile Redis only
from trusted settled receipt scopes using existing reservation logic, not by
guessing an amount from a diagnostic row. Unknown usage blocks intersecting SQL
scopes independently of Redis. Derived cache recovery cannot authorize spend
without the SQL transaction; strict policy accumulators require complete trusted
reconstruction. No marker expiry removes a durable barrier.

### Acknowledgement and crash boundary

Buffer only the final success event (candidate 256 KiB cap, not the complete
answer). Parse across arbitrary network chunk boundaries and preserve the
provider event payload/sequence. When trusted terminal usage arrives, write and
acknowledge the final receipt, then forward success. For Chat, `message_stop`/
translated finish and `[DONE]` must be coordinated by the same finalizer; do not
emit `[DONE]` early. Tool-call/Anthropic/native Bedrock streams retain their own
valid terminal forms; capacity scenarios in this epic require textual content,
but the gateway must not reject legitimate tool-only responses globally.

Proposed `BG_METERING_JOURNAL_ENABLED=false`,
`BG_METERING_HANDOFF_TIMEOUT_SECONDS=3`,
`BG_METERING_TERMINAL_MAX_BYTES=262144`; fail startup if journal mode lacks its
bucket/reader prerequisites. SDK timeouts and at most two storage attempts fit
the one deadline. Close upstream promptly at true completion; do not hold its
connection during SQL settlement. Client EOF after terminal should require only
bounded local cleanup (candidate p95 ≤1 second); terminal acknowledgement delay
is measured separately so moving delay before terminal cannot hide it.

| Failure window | Required behavior and evidence |
|---|---|
| Admission write fails | No provider invocation; CAS-cancel only the unarmed SQL attempt or persist proof of irreversible guard cancellation; return dependency failure. |
| Crash after SQL prepare, before arming | SQL record survives S3/Redis loss. Fence the old producer epoch and CAS-cancel the never-armed attempt; no inference retry. |
| Quote expires/generation changes during journal or transport wait | Final dispatch guard refuses before sending. Release only proven unsubmitted work; failure/ambiguity retains the barrier. |
| Crash after dispatch arming, before known provider outcome | Discoverable possibly-dispatched attempt; no recovery re-dispatch. SQL admissions count its bound and refuse intersecting scopes when unknown/overdue, without waiting for a scanner. |
| Provider finishes but receipt is not durably recorded | **Contained unknown usage.** Retain the durable attempt and block affected spend. No invented charge, administrative release or exact-recovery claim. Apply a trusted late receipt once if it arrives. |
| Receipt acknowledged; crash before client terminal or SQL commit | Recovery reads immutable receipt and settles once; client may report failure while billing remains correct. |
| SQL commits; acknowledgement or Redis reconciliation lost | Receipt/debit/usage/hold transition was atomic. Replay verifies owner/amount/allocation; no duplicate charge or row. Retry only trusted reconciliation; cache misses do not undo SQL state. |
| S3 timeout with ambiguous write outcome | Read back/compare within bound; otherwise fail stream without false success and leave recovery record. |
| Client disconnect or shutdown | Close upstream; bounded shield for measured receipt; propagate cancellation; unfinished admission remains visible. No detached task alone constitutes durability. |

**D2 review resolution:** the assistant-authored original AC-02 was too broad.
The [corrected recovery contract](accounting.md#r2-conservative-recovery-boundary)
preserves exactly-once settlement for durable trusted receipts and requires
explicit unknown state with affected spend blocked when usage was lost before
persistence. No risk exception, write-off, zero-cost assumption or inference
replay is included. The acceptance ID is retained; the review record quotes the
correction so downstream developers cannot mistake it for exact token recovery.

### Bounded recovery and retention

Notifications are at-least-once and can be delayed. Add tracker-specific failure
destination, alarm and bounded scheduled recovery; existing pricing-refresh
failure queues are not a tracker recovery policy. Reuse their infrastructure
pattern, with separate scoped resources. Proposed execution retry budget: two
retries / one hour event age, then quarantine plus operator action. The durable
SQL attempt index, not S3 listing, enumerates outstanding work every minute:
pages of ≤100 records, ≤1,000 records and ≤30 seconds per invocation. Claim work
with CAS lease/epoch, close SQL, then fetch exact journal keys; commits fence stale
recoverers. Pending rows stay eligible regardless of cursor position or period.
S3 partition listings are optional bounded orphan audits, never admission proof.
The consumer proves scan throughput exceeds receipt rate before activation;
increase parallelism only inside the DB budget. A failed scan resumes, and one
quarantined scope cannot monopolize recovery. Synchronous SQL admission checks
still block unknown/overdue attempts while this worker is unavailable.
Retain records until SQL and reservation receipts verify completion and the
configured retention period passes. Candidate metering retention 30 days;
unresolved/quarantined records are exempt from expiry, with alert/escalation.
Do not inherit transcript lifecycle deletion unchanged: S3 has no negative-prefix
exception to an existing all-object expiration rule. Replace the broad rule with
positive retention-class tag filters, tag new optional transcripts explicitly,
and separately tag metering objects as pending/settled only after verified
reconciliation. Before enabling journal writes, review/backfill legacy transcript
tags with an ownership-safe dry run; never classify a journal object as a
transcript by its tenant name. Untagged legacy objects retain data longer until
that step completes, an explicit storage-cost/retention tradeoff for the operator.
Gateway needs create/read-back on journal objects; recovery needs exact-key reads
and retention tagging, plus prefix-limited listing only if orphan audit is enabled.
Recovery state writes use the SQL authority, not mutable S3 gap objects. Tracker delivery needs exact
bucket/source-account conditions. No public caller gains a journal read API.
Backlog age target ≤60s normally, hard alert at 300s; unknown/quarantined or overdue
attempts block intersecting admissions immediately when observed by SQL, not
only after an alert threshold. Normal active bound accounting and completion
deadlines follow the authority contract. Thresholds are proposed, not current SLAs.

## Provider stream reliability — D3

The current same-account boto clients are long-lived; routed clients are created
per call with explicitly supplied destination credentials. The streaming read
timeout is 300s in executable config, despite an older docstring saying otherwise
([S09](audit.md#source-evidence)). SDK reads run through `asyncio.to_thread`; a
cancelled await does not necessarily stop the underlying synchronous read.

First instrument client age/reuse class, SDK version/config, queue wait, dispatch,
headers/first chunk/first content/terminal, close reason, exception class and SDK
attempt count. Compare cold, warmed, idle-reused, long-lived and concurrent clients
through the real Chat route with short/long, cached/unique workloads. Direct
InvokeModel controls must match model/prompt/SDK settings and are controls only.
Reuse, socket timeout, executor starvation, edge timeout and provider failure
remain hypotheses. Do not enlarge every timeout or pool by default.

Use a state machine: admitted → dispatched → content → terminal-success or
terminal-failure; cancellation/disconnect may interrupt any state. EOF without a
provider terminal fails even after content. Malformed/error events must not be
silently skipped into success. Close body and routed disposable clients in every
path; verify another healthy stream proceeds after failure. Same-account client
replacement must be bounded and must not disturb active users. Never cache
destination clients under a weaker identity key than the existing signer.

Default qualifying inference retries: zero. For any future pre-content retry,
distinguish a proven not-dispatched failure from an uncertain accepted invocation;
“no content received” alone is not proof that replay is safe. Every physical SDK
attempt consumes campaign attempts and token/cost headroom. No retries after
partial delivery. Preserve actual terminal error events on Responses; use
protocol-appropriate error frames only at valid boundaries, otherwise close and
record failure. No fabricated `response.completed`, `[DONE]`, or `message_stop`.

## Scaling, readiness and overload — D4

The opt-in comparison profile is two workers, two idle pods, CPU request 1 core /
limit 2 cores, memory request 1 GiB / limit 2 GiB, absolute CPU target 1000m,
ceiling 12; downscale stabilization 120s, at most one pod/minute. Its 30s pre-stop,
960s grace and PDB remain intact during comparison. Default manifests differ;
neither ceiling is a capacity guarantee ([S10–S11](audit.md#source-evidence)).

Measure demand detection → HPA decision → scheduling → node ready → image pull →
init/instrumentation → app ready → first admitted request. Report desired,
scheduled, ready and serving capacity independently. A static `/health` endpoint
is current behavior, not a dependency health check. Add startup readiness for
local initialization, liveness for process/event-loop progress, and draining
readiness to stop new admissions. Do not make shared SQL/Redis outage a liveness
restart condition or remove every pod from service; return controlled admission
failures while reporting degraded dependency health separately.

Maintain bounded per-worker admission/stream permits and a short bounded queue,
released in all exits, without a new DB transaction per permit. Proposed
`BG_MAX_ACTIVE_STREAMS_PER_WORKER` and `BG_STREAM_ADMISSION_QUEUE_LIMIT` have no
invented production defaults; #7034 must choose them from comparison results.
At saturation reject before provider dispatch with explicit overload status and
bounded retry guidance. Distinguish this system protection from existing tenant
concurrency quotas; never raise normal-user quotas to improve the score.

```text
gateway_connections = (ready_ceiling + surge + terminating_overlap)
                      × workers × (request_pool_max + accounting_pool_max)
total_connections = gateway_connections + tracker_concurrency × tracker_pool_max
                    + other_services + migration/admin_reserve
total_connections ≤ database_usable_connections − safety_headroom
```

At 12 pods × 2 workers × (5+5 current maximum), the gateway alone permits 240;
one surge makes 260 before terminating pods and other consumers. Proposed 6+4
split keeps the same per-worker total, not an entitlement to those connections.
Inspect actual database maximum/reserves privately. Default NullPool does not
provide this bound and needs separate concurrency accounting. Bound rollout
overlap explicitly; long terminating streams can outlive a nominal surge slot.

Evaluate CPU-only against active-stream/queue-based policies; add a custom-metric
adapter only if measured improvement justifies its operation and failure modes.
Prewarm for a burst whose arrival rate exceeds ready headroom during the measured
startup delay, or reject boundedly. Publish a policy indexed by model/API,
cache/context mix, identity distribution, fan-out, per-pod safe throughput,
startup p95/p99, and the chosen latency SLO—not “six pods = 200 developers”.

## Diagnostics — D5

Reuse timing/tracing and EMF; define one lifecycle event contract shared by
#7031/#7032/#7033/#7036: monotonic durations for admission, DB checkout/transaction,
executor queue, provider first-content/terminal, durable handoff, SQL settlement,
reservation reconciliation, terminal sent and server EOF. The client records
first-content, terminal received and EOF independently. Do not subtract absolute
timestamps across hosts as if their clocks were synchronized.

Metrics: active streams, admission rejects/queue, pool checked-out/max/waiters,
checkout timeouts, transaction/lock age, event-loop lag, provider outcomes and
attempts, pending/unknown/quarantined accounting, exporter failures/drops,
audit-write failures, ready replicas and restarts. Restrict dimensions to stable
model/API/operation/outcome classes; tenant IDs, request IDs, query text, tokens,
endpoints and credentials are not metric labels. Protected traces may correlate
an attempt; public reports aggregate categories only.

Use a separately scoped diagnostic connection/role, sampling active waits at a
candidate 1s interval only during bounded reproductions and 5s aggregates during
qualification. Limit rows (100), query duration (500ms), retention (7 days
candidate private diagnostics) and collector concurrency (one). Prove diagnostics
do not consume the serving pool or become a lock participant. Trigger active
blocking-graph capture on checkout pressure, not after campaign termination.

CloudWatch EMF namespace `BedrockGateway` and direct API namespaces are different
paths. Identify the denied call/actual role before changing IAM. For
`cloudwatch:PutMetricData`, AWS has no resource-level ARN: the necessary
`Resource: "*"` exception must be constrained by exact `cloudwatch:namespace`
conditions on the actual producer role; no `cloudwatch:*`. Use existing
orchestration/pricing policies as precedents. Verify data arrival and intentional
export failure separately from inference success. Audit warnings need independent
delivery evidence before claiming lost records or an inference cause.

## Integration order, deployment and rollback

No environment changes are part of this PR. Deployment requires a separately
authorized selected connection and named operator following the canonical
[agent deployment guide](../../adp-platform-deployment/deploy-with-agent.md).
No new AWS privilege is implied by a design or a connected workload role.

1. **Approve decisions and name owners.** Keep the conservative D2 contract and root-cause gates visible.
   Land diagnostics and no-cloud harness capabilities first; no billed PR jobs.
2. **Diagnose and contain.** #7031 and #7033 reproduce their failures with the
   bounded discovery campaign. Coordinate proxy/database edits with #7032.
3. **Prepare authority/recovery before producers.** #7032 deploys additive SQL
   migrations, compatible gate-aware writers/readers, bounded recovery, failure
   destinations, IAM, notification suffixes and lifecycle exceptions. Drain a
   closed set of shared scopes and verify its baseline before activation; legacy
   ungated writers cannot serve active scopes. Retain legacy receipts/read formats.
   No journal flag until authority, dispatch guard and recovery are verified.
4. **Canary behavior.** Enable bounded transactions, truthful terminals and journal
   mode for owned test traffic on an approved target. Verify denial, cancellation,
   tenant separation, restart recovery and reconciliation before increasing load.
5. **Integrate scaling selection.** #7034 renders a selected profile in both
   workflow and launcher, tests successive deploys for persistence, then compares
   measured policies without increasing the connection budget.
6. **Qualify and report.** Named owner performs campaigns, cleanup and live child
   acceptance at exact deployed image/configuration. Use `Refs #7030`/story
   references, not automatic issue closing, until required live ACs pass.

Gateway source/images and tracker packages use the existing
[`gateway-deploy.yml`](../../../.github/workflows/gateway-deploy.yml#L5) pipeline;
`main` pushes are path-filtered and manual dispatch also exists. Gateway and
platform IAM/Terraform changes require the separately manual infra-apply paths.
The launcher/build/rollout matrix is in [the audit](audit.md#deployment-and-build-entry-points).
Docs-only changes do not deploy the gateway. Preserve source/image verification,
pre-serving migrations, pricing rollout coordination and frontend compatibility;
do not create a performance bypass deployment script.

**Rollback:** stop new test admissions, drain up to the campaign bound, save
failure evidence and restore the last verified image plus rendered profile and
config. Do not disable recovery or drop receipts while journal records remain.
Reader-first upgrades must retain both formats until old producers and pending
records retire. A gateway rollback may disable new journal production only after
the operator verifies recovery continues and durable holds are intact. Old
binaries must not serve active scopes without the new gate; retain additive
tables/aliases/audits and stop affected admissions rather than bypass them. If
the former release lacks the required durability guarantee, stop affected traffic
rather than claiming a safe rollback. Do not downgrade/drop settlement tables or
erase unknown events. Restore scoped test quotas with compare-and-swap ownership
checks and verify baseline service health. Infra rollback is an explicit reviewed
plan, not “revert the PR”; code-only rollback does not undo IAM/notification or
lifecycle changes. The named operator owns the rollback receipt.

## Completion boundary

This proposal ends at a ready design PR and requester review. All implementation,
root-cause reproduction, capacity thresholds, deployed validation and fixture
cleanup receipts remain future work. [Campaign acceptance](campaigns.md#acceptance-and-reporting)
maps epic AC-01–05; [story designs](stories.md) preserve every child AC. Missing
live evidence remains blocked/not-run, not a pass or an implied approval.
