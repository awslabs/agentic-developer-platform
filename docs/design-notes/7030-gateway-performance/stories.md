# Story designs and implementation backlog

**Approval is tracked against the exact final design commit in PR #7039.** Reuse the six existing children of
[#7030](https://github.com/aws-e/adp/issues/7030); create no duplicate issues.
The [epic design](README.md) owns shared invariants and proposed decisions;
[campaigns](campaigns.md) own cost/cleanup and success rules. Code references are
verified against the [audited revision](audit.md), not a claim of deployment.
The [accounting contract](accounting.md) is part of each affected Design section,
not an optional follow-up. R2 retains unknown usage with affected spend blocked;
financial exceptions are excluded. AC-02 is explicitly corrected in the review
record. Incorporate these designs into existing issues after the final approval.

For **every story**, executor and live-validation owner are **unassigned**; the
epic coordinator must record actual names before dispatch. Completion means
implemented, reviewed, deployed and live-verified. Code review/CI can finish
before live acceptance, but blocked/not-run live rows keep the issue open. Use
`Refs`, not automatic closing references. No dispatch or deployment is requested
by this design PR.

## Dependency and shared-file agreement

| Order / checkpoint | Outcome | Depends on / readiness |
|---|---|---|
| 1: #7035 diagnostics + #7036 harness foundation | Reliable observations and safe, recoverable smallest campaign | Can be prepared independently after proposal approval/owners; no prerequisite to inspect source. |
| 2: #7031 blocker investigation + #7033 transport investigation | Root-cause evidence and bounded local regressions | Live reproduction needs diagnostics, harness/cleanup and separate target authority. |
| 3: #7031 containment + #7032 durable finalization + #7033 reliability fix | Coordinated transaction/terminal/accounting contracts | #7031 chooses SQL fix only after diagnosis; #7032 implements the conservative D2 contract; #7033 selects transport change only after attribution. |
| 4: #7034 policy comparison + #7036 representative matrix | Measured readiness, overload and honest workload coverage | Defect fixes deployed; diagnostics working; predeclared latency SLO. |
| 5: all owners / coordinator | Qualifying campaigns, child receipts and final capacity report | All mandatory child/epic ACs and cleanup pass at recorded revision. |

| Shared surface | Integration owner | Required collaborators |
|---|---|---|
| `shared/database.py`, pool/timeout settings, transaction boundaries | #7031 | #7032 consumes accounting factory; #7035 adds hooks without owning transactions. |
| `proxy/service.py`, `proxy/mantle_service.py`, `proxy/routes.py`, terminal coordinator | #7032 | #7033 supplies provider state/error handling; agree final event ownership before concurrent edits. |
| `pool/simple_pool.py`, `proxy/stream_handler.py`, provider reader/translator | #7033 | #7032 durable-before-success boundary; #7036 parser expectations. |
| `usage/service.py`, `budget/settlement.py`, reservation recovery, tracker/record schema | #7032 | #7031 lock ordering/timeouts and #7035 accounting-health signals. |
| New metering authority/migrations, budget/person/identity mutation fences, reconciliation admin API | #7032 | #7031 transaction cost/lock review; #7035 observability; #7036 fault cases. No competing admission ledger in another story. |
| `orchestration/provider_quotes.py`, `budget/pricing_v2_reader.py`, final dispatch permit/guard | #7032 contract, #7033 transport integration | Preserve #5225 checks, coordinate pricing publication with transport handoff, test generation/expiry after all awaited preparation. |
| Gateway/platform IAM, shared metrics/tracing | #7035 | #7032 owns new journal/recovery permissions and queues; #7034 owns capacity signals. Serialize edits in shared Terraform files. |
| Deployment rendering, profiles, readiness and capacity config | #7034 | #7031 connection limits; #7032 drain; #7035 metrics; preserve normal deployment authority. |
| Performance controller/parser/fixtures/reports | #7036 | All producers use its versioned evidence schema, not private ad-hoc scripts. |

The coordinator selects one integrator per shared file at assignment time.
Adjacent changes must land against that shared contract, not overwrite each
other. Tests in these paths may be amended by their owners; new tests described
below are deliverables, not falsely presented as existing files.

## #7031 — contain database lock and pool failures

### Description

Find the blocking transaction and prevent it from holding the request-serving
database capacity hostage. Discovery is part of this story, not deferred to
#7032 or solved by increasing connections. Ready only for **proposed bounded
investigation** until the blocker is known; no table-specific fix is approved.

### Impact analysis

All caller authentication/approval/budget/rate-limit reads and metering share
database resources. A too-short timeout can deny healthy traffic; a too-large
pool can overload the database across workers and rollout overlap. Failures
must remain fail-closed and tenant-scoped. The additive admission schema belongs
to #7032; #7031 reviews its lock/connection costs without guessing that it fixes
the original blocker. No new identity format. Migration/admin connections must
not inherit runtime limits.

### Design

Verified entry points: [engine/session creation](../../../modules/gateway/src/shared/database.py#L107),
[runtime settings](../../../modules/gateway/src/shared/config.py#L22),
[auth dependency](../../../modules/gateway/src/auth/middleware.py#L76),
[actual proxy context](../../../modules/gateway/src/proxy/routes.py#L228),
[approval session](../../../modules/gateway/src/auth/approval_middleware.py#L195),
[rate-limit factory](../../../modules/gateway/src/ratelimit/service.py#L80),
[budget enforcement](../../../modules/gateway/src/budget/enforcement_service.py#L1),
[usage commit](../../../modules/gateway/src/usage/service.py#L127) and
[settlement ordering](../../../modules/gateway/src/budget/settlement.py#L16).
Do not assume the legacy auth dependency is the streaming route's blocker: the
proxy first consumes middleware-verified context. Trace the actual campaign path.

First checkpoint: a reproducible PostgreSQL blocker/waiter graph with operation
fingerprint and transaction lifetimes, comparing shared user / distinct users
same tenant / distinct tenants. Capture while blocked, including cancellation
and any network await inside a transaction. Explain whether contention is same
receipt, same aggregate ledger row, auth mutation, or another path; “pool timeout”
is a symptom, not the diagnosis. If unavailable, leave AC-01 incomplete.

Implement D1's purpose-specific factory and opt-in finite timeouts only after
the checkpoint selects required boundaries. Keep total candidate pool maximum
10/worker (6 request +4 accounting, zero overflow), not 10+10. Default-off
compatibility and password/IAM/SQLite branches need explicit tests. Export pool
checkout/hold times without tenant labels. Use transaction-local lock/statement
limits and close/rollback on exception or cancellation; rethrow cancellation.
Invalidation is for an unusable connection, not a blanket reset of every worker's
engine. Any retry creates a fresh transaction, uses the original deadline and
checks ambiguous prior commit by receipt first. No generic retry of auth denial
or conflicting settlement payloads.

For the original blocker no migration is preselected. If diagnosis needs an index or changed lock order,
record the actual query/plan fingerprint, evaluate write amplification and
concurrent migration behavior, and update the approved design before widening
schema scope. D1's error mapping distinguishes transient dependency 503 from
invalid credential 401/forbidden 403 and quota 429.

Coordinate the new [authority transactions](accounting.md#atomic-transitions-and-request-path-check)
with #7032: sorted scope gates → attempt → settlement receipt → aggregates;
reserve/arm in request pool, settlement in accounting pool, no network work inside
either. Verify all denomination writers and cap/identity mutations participate.
Add real PostgreSQL tests for simultaneous admissions after Redis flush/expiry,
shared organization and cross-org person contention, concurrent settlement, SQL
commit ambiguity and cancellation. Measure two added pre-dispatch transactions,
lock/checkout tails and unrelated-scope progress; no new pool or cap increase.

### Deployment

Named operator deploys through the gateway workflow with split mode initially
disabled, applies private candidate config, canaries fault tests, then enables
owned traffic. Preserve IAM token refresh and TLS. Roll back code/config only
after draining and checking no accounting worker owns a live transaction;
restore previous **combined** connection limits. Schema rollback, if later
required, needs its own additive/forward-compatible plan. #7032 integration
must not restore long SQL work to the HTTP finalizer.

### Validation

Extend [IAM/pool tests](../../../modules/gateway/tests/shared/test_database_iam.py#L1)
and add real-PostgreSQL request-path contention fixtures; SQLite and mocked
sessions cannot prove transaction-lock behavior. Exercise two tenants and
revoked/denied callers while one operation is blocked, both cancellation owner
and waiter cases, server timeout, expired IAM replacement, lost DB connection,
pool saturation, and repeated healthy recovery without a growing checked-out
count. Assertions observe outcomes/DB state, not only configuration strings.

| Existing ID | Required result / evidence | Phase and owner |
|---|---|---|
| AC-01 | Real PostgreSQL contention identifies responsible transaction or explicitly remains incomplete; no tenant/auth bypass. Sanitized reproduction plus protected graph. | Before fix review, named executor. |
| AC-02 | Cancellation/timeout rolls back, returns or invalidates connection and healthy traffic recovers within configured bound, no persistent pool leak. Request-path integration plus pool/transaction metrics. | Before review, named executor. |
| AC-03 | Deployed shared/distinct identity ramps meet ≥99% valid completion at target without unbounded waits/cascading exhaustion. Full stage/error/DB report. | After deployment, named live owner. |

## #7032 — bounded finalization with recoverable accounting

### Description

Separate model generation, durable accounting handoff and ledger settlement so
a caller does not wait on unbounded SQL after receiving model output. Coordinate
with #7031 before shared transaction/schema changes. Protocol, schema and fault-test
preparation can start after design approval. The explicitly corrected AC-02 keeps
unknown pre-receipt usage blocked; it never promises to reconstruct lost tokens.

### Impact analysis

The change touches success-terminal timing, budget exposure, pricing and optional
chat logs across both APIs. Incorrect replay can double-charge; wrong attribution
can cross tenants; a lost receipt can undercharge. S3 becomes a required handoff
dependency in journal mode. Preserve disabled-mode compatibility, existing pricing
generations, cache accounting, reservation trust and persona/run attribution.
SQL is also mandatory for active admission. The unknown barrier remains effective
in observe/off cap modes; legacy ordinary/person fallback is not allowed to
bypass it. Activate only closed shared-scope cohorts after draining old writers.

### Design

Verified entry points: [Responses finalizer](../../../modules/gateway/src/proxy/mantle_service.py#L556),
[Responses metering](../../../modules/gateway/src/proxy/mantle_service.py#L763),
[Chat metering](../../../modules/gateway/src/proxy/service.py#L479),
[logging wrapper](../../../modules/gateway/src/chat_logging/service.py#L596),
[S3 writer](../../../modules/gateway/src/chat_logging/s3_writer.py#L165),
[usage write](../../../modules/gateway/src/usage/service.py#L127),
[settlement helper](../../../modules/gateway/src/budget/settlement.py#L16),
[reservation recovery](../../../modules/gateway/src/budget/reservations.py#L529),
[tracker](../../../modules/gateway/lambda/budget-usage-tracker/handler.py#L367),
and [pricing verifier](../../../modules/gateway/lambda/shared/pricing_settlement.py#L1).

Also integrate [budget admission](../../../modules/gateway/src/budget/enforcement_service.py#L1089),
[person resolution](../../../modules/gateway/src/budget/person_ledger.py#L205),
[identity linking](../../../modules/gateway/src/shared/identity/workspaces.py#L156),
[legacy usage writer](../../../modules/gateway/src/budget/service.py#L288),
[budget admin controls](../../../modules/gateway/src/budget/enforcement_routes.py#L53),
[person-cap authority](../../../modules/gateway/src/budget/person_cap_routes.py#L39)
and [quote checks](../../../modules/gateway/src/orchestration/provider_quotes.py#L709).
Implement the approved [five additive tables and state transitions](accounting.md#additive-schema),
period-independent hierarchy/person/policy gates, alias preservation, atomic
headroom checks, dispatch arming and receipt/hold settlement. These are required
new interfaces, not functions already provided by S3 or `BudgetAccountingGap`.
Extend true bounded quotes to any supported route lacking an adapter; unsupported
features fail explicitly rather than use a guessed upper cost. Own the proposed
admin reconciliation API, including human authority, evidence validation, CAS
audit and late-receipt races. No financial exception action or enablement flag is delivered.

Implement the approved version of D2's admission/final receipt APIs, versioned
envelopes, immutable S3 suffixes and canonical hash checks; no transcript content
or credential data. Make mandatory metering independent of `should_log()` and
its model exclusion list. Keep optional transcript behavior separate. Capture
trusted context/price/route once; no membership or model-price re-resolution on
replay. Reserve before provider call; never interpret missing usage as zero.

One finalizer owns terminal ordering across Chat/Responses and closes upstream
before SQL settlement. Buffer only the terminal within the cap; arbitrary SSE
splits, terminal plus following bytes in one network chunk, sequence numbering,
valid tool-only terminals and invalid/missing usage need tests. On handoff
timeout, bounded read-back may recover acknowledgement; otherwise emit a safe
failure at a valid frame boundary, no completed terminal, and retain discoverable
accounting uncertainty. Cancellation is propagated after bounded cleanup, with
no free-running untracked task treated as durability.

Extend tracker readers before producers; atomically deduplicate usage insertion
under the existing settlement receipt as well as debit, atomically with hold
disposition using the proposed additive admission schema. Durable
receipt recovery, SQL-commit acknowledgement loss and Redis failure have different
states. Add bounded scan/cursor/lease, tracker-specific failure destination,
unknown/quarantine alerts and lifecycle exceptions. Enumerate recovery from the
SQL attempt index, not an S3 scan assumed complete. #7032 owns journal IAM in
the existing bucket and tracker role: exact bucket/object-prefix/queue/KMS scopes,
resource policies and invocation source conditions, never an all-S3 grant.
Neither hosted worker nor ARC role needs journal access; only gateway, tracker
and the authorized recovery principal do. Webhook-ingress needs no new grant.

### Deployment

Named operator applies approved additive migrations/infra before activation,
deploys all compatible gate-aware readers/writers, verifies legacy/new fixtures,
then drains and reconciles the full shared-scope cohort before active journal mode.
Old ungated producers cannot continue within it; retain disabled-mode legacy
behavior outside activated cohorts. Package pricing/shared contracts
through the existing Lambda archive builder and gateway deployment pipeline.
Do not bypass pre-serving migration/pricing coordination. Rollback stops new
producers but retains readers, immutable records and SQL receipts until reconciled;
old code ignoring additive fields is not permission to delete records. Restore
optional transcript flags without disabling mandatory recovery. Only the named
operator supplies deployment/rollback/live reconciliation receipts.
Rollback to a binary lacking the gate requires affected spend stopped; retain
tables, aliases, receipts and audits rather than destructive down-migration.

### Validation

Extend [Responses lifecycle tests](../../../modules/gateway/tests/proxy/test_mantle_stream_lifecycle.py#L1),
[settlement tests](../../../modules/gateway/tests/proxy/test_mantle_responses_settlement.py#L1),
[receipt tests](../../../modules/gateway/tests/budget/test_settlement_receipts.py#L1)
and [tracker contract tests](../../../modules/gateway/tests/lambda/test_pricing_settlement_contract.py#L1).
Use process-kill tests at every crash-table boundary, not merely an exception
mock. Duplicate/out-of-order S3 delivery, conflicting owner/price/hash, optional
logging off, oversized terminal, unknown usage, delayed SQL/Redis, cancellation,
acknowledgement loss, restart and expiration all require observable recovery.
Run all [accounting/dispatch fault cases](campaigns.md#accounting-and-dispatch-fault-matrix)
through real routes with PostgreSQL/Redis and controlled provider/storage faults.
They include process kills, frozen recovery, unknown-usage containment, forbidden
manual release, cross-org identity fusion and quote rollover during journal I/O.
Passing proposal/link checks is not evidence these invariants are implemented.

| Existing ID | Required result / evidence | Phase and owner |
|---|---|---|
| AC-01 | Real Responses request with delayed/failed persistence stays within documented finalization bound; valid failure/success semantics; pending accounting durably discoverable. Lifecycle fault-injection evidence. | Before review, named executor. |
| AC-02 | After a crash, replay any durable trusted usage receipt and settle exactly once at the business-record level, without duplicate or cross-tenant charges. If provider usage was lost before durable receipt storage, retain the durable attempt as unknown and block affected spend until trusted evidence resolves it; do not invent usage, treat it as zero, release it by administrative exception, or replay inference. | Before review, named executor; F01–F12/F14 distinguish durable recovery from unknown containment. |
| AC-03 | Deployed Astra ramp meets predeclared SLO and epic threshold, reports provider versus gateway timing and reconciled usage without indefinite backlog. | After #7031 integration/deployment, named live owner. |

## #7033 — truthful, reliable Opus streams

### Description

Reproduce and fix the gateway stream failure or clearly attribute an external
failure while preserving explicit error semantics. Investigation can proceed
independently; transport fix selection is blocked on evidence. Direct control
success, #4810's hosted watchdog or #964's non-streaming timeout is not the cause
by assertion.

### Impact analysis

Connection replacement/retries can duplicate expensive inference, leak threads
or reuse another caller's credentials. Tight deadlines can reject valid slow
models. Retain explicit destination signing boundaries and existing supported
Chat/Anthropic/native Bedrock formats; do not implement a broad provider rewrite.

### Design

Verified paths: [client factory](../../../modules/gateway/src/pool/simple_pool.py#L47),
[provider iterator](../../../modules/gateway/src/proxy/service.py#L844),
[SSE translation](../../../modules/gateway/src/proxy/stream_handler.py#L142),
[Chat route](../../../modules/gateway/src/proxy/routes.py#L389),
and [terminal parser](../../../tests/performance/gateway/locustfile.py#L125).
Instrument actual client reuse class, executor queue/SDK attempts and exceptions
using #7035's contract. Run bounded cold/warm/idle/concurrent/long-context tests
through the real route with matched direct controls. Identify transport boundary
for every failed sample; uncorrelated failures remain unknown, not provider blame.

Fix EOF-to-success translation regardless of whether it explains this campaign:
track provider terminal and reject incomplete/error events; coordinate terminal
ownership with #7032. Add safe closure of event body and disposable routed client,
bounded cancellation of synchronous readers and subsequent healthy-call recovery.
If reproduction justifies client/HTTP pool changes, document effective SDK defaults,
identity key, lifecycle, socket/FD/thread limits and measured effect. Do not
preselect stale-connection remediation or bulk client recycling.

Default no inference retry after dispatch/uncertain outcome; never after partial
content. A future proven pre-dispatch retry needs one original deadline and
attempt/cost permits from #7036, with no hidden SDK multiplication. No new SQL
schema, identity field or deployed timeout is preapproved. Preserve non-streaming
behavior except a separately reviewed necessary shared-client fix.

Own transport integration of the [final quote/permit guard](accounting.md#r3-final-dispatch-and-quote-validity)
with #7032. Place it after executor/connection/signing preparation and before
request submission, not merely before an awaited SDK call. Expose irreversible
no-send cancellation proof and ambiguous-send outcomes to the authority; no replay
or release based on absent headers. Test expiry/generation changes during S3 and
transport waits, one-use permit fencing and unchanged historical receipt pricing.
If a transport cannot provide the required boundary, keep its journal-mode
activation blocked until the adapter does; do not assert the hook exists today.

### Deployment

Named operator canaries both gateway APIs, then repeats Opus ramp only after local
fault cases and #7031 containment pass. Gateway workflow ships service/client/
translator together with compatible #7032 terminal handling. Roll back the
coordinated producer version, not one incompatible translator; continue receipt
recovery. New timeouts/client limits, if selected, are private explicit config
with previous settings retained for rollback. No agent runtime/watchdog deploy.

### Validation

Extend [Bedrock errors](../../../modules/gateway/tests/proxy/test_bedrock_stream_errors.py#L1),
[stream handler tests](../../../modules/gateway/tests/proxy/test_stream_handler.py#L1)
and [loopback parser tests](../../../tests/performance/gateway/test_stream_validation.py#L1).
Inject failure before headers, before content, after content, inside a frame,
normal EOF without terminal, malformed data and provider exception events.
Check errors and absence of success markers, exact invocation count, bounded
reader cleanup, no leaked connection and no cross-credential client reuse.

| Existing ID | Required result / evidence | Phase and owner |
|---|---|---|
| AC-01 | Real Chat route failure before/after content is observable, no false completion/duplicate generation/connection leak. Integration and parser regressions. | Before review, named executor. |
| AC-02 | Cold/reused/concurrent gateway clients reproduce repaired cause or clearly attribute external failure; direct controls alone insufficient. Correlation report. | Before live qualification, named executor. |
| AC-03 | Full Opus ramp, five minutes at 200, ≥99% complete streams, no restart/OOM. Every skipped stage remains incomplete. | After deployment, named live owner. |

## #7034 — measured readiness and capacity policy

### Description

Explain scale-out delay and produce an evidence-backed capacity policy rather
than increasing replicas blindly. Readiness discovery can proceed after approval;
final policy depends on defect resolution and chosen SLO.

### Impact analysis

Worker counts, pool maxima and surge/termination overlap determine DB pressure.
CPU versus stream metrics have different scale-out lag. Shared dependency outages
must not cause restart storms; bounded overload may intentionally reject traffic
before inference rather than overload every dependency. Retain the two-pod floor
unless explicit evidence and approved amendment justify another value.

### Design

Verified files: [base deployment](../../../modules/gateway/k8s/deployment.yaml#L1),
[base HPA](../../../modules/gateway/k8s/hpa.yaml#L42),
[profile](../../../modules/gateway/k8s/profiles/llm-proxy/README.md#L1),
[health handler](../../../modules/gateway/src/app.py#L460),
[workflow manifests](../../../.github/workflows/gateway-deploy.yml#L683),
[launcher manifests](../../../platform/scripts/deploy-all.sh#L1251)
and [rollout helper](../../../platform/scripts/gateway-rollout.sh#L1).

Produce timestamped node/scheduling/image/init/app-ready/first-service breakdown,
per-pod active/queued streams, resource pressure and DB headroom. Compare existing
CPU profile against evidence-selected alternatives under the same source/image,
instrumentation and workload. Implement local readiness/drain and bounded
admission as specified in D4; add custom-metric infrastructure only if selected
with explicit failure behavior. No schema change. Per-worker stream/queue limits
and final CPU/HPA settings remain unresolved until comparison; never invent a
per-pod concurrency claim from the supplied six-pod observation.

Proposed deployment configuration `gateway_capacity_profile=default|llm-proxy`
must be validated and rendered consistently by both workflow and launcher using
one shared renderer, preserving security/probes/instrumentation and selected
image. Existing environments stay default unless explicitly selected. Assert
repeated apply/upgrade retains the profile rather than resetting it. Include
surge and draining old pods, Lambda consumers and admin reserve in the DB budget.

Include [authority costs](accounting.md#costs-compatibility-and-recovery) in every
policy comparison: prepare/arm transactions, shared person/org serialization,
settlement/recovery load and the final one-second candidate dispatch permit.
Admission failure due to authority pressure is not spare provider capacity.
Verify scoped unknown denial does not trigger liveness restarts or erase holds
through autoscaling, rollback or period changes. Re-measure readiness with the
additive schema/reader prerequisites; no pre-change capacity extrapolation.

### Deployment

Named operator applies a compatible image/profile through the existing workflow
or launcher; Terraform changes need their manual apply path. No manual HPA disable
or force-downscale as a success step. Restore prior rendered Deployment/HPA/PDB
and limits on rollback, keep stream drain and metering recovery alive. Observe
normal cooldown to two pods and baseline health, or report its finite observation
deadline exceeded. Source deployment success alone is not the live report.

### Validation

Extend [deployment rendering tests](../../../platform/scripts/tests/test_gateway_deployment_render.py#L1)
and [rollout tests](../../../platform/scripts/tests/test_gateway_rollout.py#L1).
Add policy/profile persistence, invalid profile, new/old producer compatibility,
overload permit release, dependency outage without liveness storm, and active
stream drain tests. Test missing metrics behavior. Live burst/soak comparisons
use [finite campaign plans](campaigns.md#finite-plans-and-implementation-checkpoints).

| Existing ID | Required result / evidence | Phase and owner |
|---|---|---|
| AC-01 | Identify measured scale-out delay stages with desired/current/ready replicas distinguished. | Discovery, named executor. |
| AC-02 | After fixes, selected policy meets SLO/success/connection bounds under ramp/burst/drain without dropped healthy streams. | After deployment, named live owner. |
| AC-03 | Normal cooldown returns to declared two-pod baseline without OOM/restart or forced success; publish resources/limits. | After deployment, named live owner. |

## #7035 — actionable, bounded observability

### Description

Restore metric delivery and capture the evidence needed to select fixes while
failures are active. Source work is independent; live IAM correction requires
identifying the actual denied writer/namespace/role. Observability denial is
not itself an explanation for incomplete inference.

### Impact analysis

Excess labels, full SQL values and raw errors can expose tenant data or create
unbounded cost. Diagnostic queries can add pool pressure. Required metering
health and optional audit/export health must remain distinct from inference.

### Design

Verified paths: [EMF](../../../modules/gateway/src/shared/metrics.py#L35),
[direct metric helper](../../../modules/gateway/src/admin/cognito_claims.py#L32),
[pricing writer](../../../modules/gateway/lambda/shared/pricing_fallback.py#L139),
[tracing](../../../modules/gateway/src/shared/tracing.py#L1),
[gateway bucket/role policies](../../../modules/gateway/infra/main.tf#L356),
[platform role policies](../../../platform/infra/modules/eks/main.tf#L490),
[namespace-scoped precedent](../../../modules/gateway/infra/modules/orchestration-tick/iam.tf#L72)
and [pricing precedent](../../../modules/gateway/infra/modules/budget-lambda/iam.tf#L83).

Use D5's shared lifecycle schema, stage metrics, bounded collector and privacy
rules; #7032 supplies durable-handoff timings. Identify whether missing data is
EMF collection, direct API denial or downstream query/config mismatch. Capture
the exact denied namespace privately, grant only that namespace on the actual
producer role, and negative-test an unrelated namespace. `PutMetricData` needs
the documented resource-wildcard/namespace-condition exception, not an invented
metric ARN. Do not modify unrelated hosted worker roles.

Add exporter liveness/failure/dropped counters and explicitly bounded buffers;
backpressure drops optional diagnostics rather than request progress. Report
metering pending/unknown/quarantined separately, never drop required receipts.
Capture active SQL block graphs using separate scoped diagnostic access with no
bind values; sanitized query fingerprints map to operation tags in source.
Audit warnings get a delivery reconciliation checkpoint; unrelated confirmed
audit defects go to their owner without implying this story fixes them.

Add low-cardinality prepare/arm/dispatch-refusal latency and outcomes, scope-kind
lock waits, unresolved/overdue age, Redis rebuild conflicts, settlement lag,
late receipts and denied administrative-release attempts. Unknown amount is not
zero spend; distinguish bounds, measured charges and unresolved observations. Receipt hash, actor,
tenant/person keys and protected evidence references belong only in authorized
audit records, not metric labels. Test exporter loss independently of the durable
authority: failed diagnostics must neither clear a barrier nor block recovery.

### Deployment

Named operator applies the correct gateway or platform role policy through its
manual infrastructure workflow, then deploys producers/collector and verifies
metric arrival under that role. No source-only IAM pass substitutes for live
delivery. Rollback instrumentation without removing receipt recovery visibility;
restore only this change's grants/config and preserve protected evidence.

### Validation

Extend [metrics tests](../../../modules/gateway/tests/shared/test_metrics.py#L1)
and adjacent infrastructure assertions. Inject missing/denied export, buffer
overflow, database blocker, incomplete stream and audit-write failure; test
redaction with seeded prompts/tokens/tenant values. Compare diagnostics on/off
under identical bounded load; proposed overhead acceptance ≤5% relative p95
first-content and throughput degradation, with sample uncertainty recorded.
This threshold needs approval, not a claim that overhead is measured today.

| Existing ID | Required result / evidence | Phase and owner |
|---|---|---|
| AC-01 | Required metrics arrive in intended namespace from actual deployment role, least required permission, missing export detected. | After deployment, named live owner. |
| AC-02 | Active lock and incomplete stream are captured/classified without public secret/prompt/tenant leakage. Redaction negatives and protected originals. | Before review and live validation, named executor/owner. |
| AC-03 | Guarded campaign and simulated export failure report exporter versus metering/audit health; overhead/retention bounded, requests not blocked by diagnostics. | After deployment, named live owner. |

## #7036 — reproducible campaigns with runner-loss cleanup

### Description

Turn the current single-identity Locust test into reproducible developer/agent
campaigns with accurate outcomes and durable fixture recovery. Independent
harness development does not wait for defect fixes; final capacity certification
does. No ad-hoc campaign data or secrets may be copied into the public repository.

### Impact analysis

Live inference costs money; retries and fan-out multiply that cost. Test quota
changes must never alter normal users. A lost runner must not leave enabled
identities, elevated quotas or cached secrets. Successful-only timing aggregates
can hide failures, and configured sessions are not distinct developers.

### Design

Extend [Locust](../../../tests/performance/gateway/locustfile.py#L1),
[parser tests](../../../tests/performance/gateway/test_stream_validation.py#L1)
and [README](../../../tests/performance/gateway/README.md#L1).
Use [campaigns.md](campaigns.md) as the versioned controller/config/report and
recovery interface proposal, including explicit exit states and immutable private
provenance. First checkpoint is one model with stage evidence, stop/drain and
cleanup, exercised through both APIs using compatible models.

Replace uncoordinated globals for multi-worker mode with centrally leased
attempt/cost permits and durable checkpoints. Keep legacy single-process behavior
explicit. Parser success requires real content plus correct terminal, never
HTTP 200 alone; exercise unsupported events, truncation, whitespace, chunk splits,
duplicate terminal, terminal error, timeout and cancellation. The controller
records stage admission and later outcome, actual concurrency and every physical
retry. Enforce maximum request/time/token/cost budgets across workers, zero
inference retries by default, and finite drain independent of pod grace.

Implement the protected mutation journal, fencing/lease, idempotent restore and
separate recovery-runner entry point **before** creating fixtures. Use supported
owned identity/quota APIs after verifying their authorization; do not write DB
rows or forge trusted headers to avoid policy. Record before/after versions and
only restore unchanged owned values. Conflicts need operator action, not blind
overwrite. The recovery owner/credential path and janitor deployment choice
remain a named-owner prerequisite; source work does not imply such access exists.

No new product DB schema is required for the controller. Its journal and raw
reports live in operator-supplied protected durable storage, separate from public
GitHub artifacts. Add deterministic no-cloud PR CI and a separately protected,
manually invoked live entry point, following existing
[manual live-test precedent](../../../.github/workflows/gateway-live-tests.yml#L1)
without treating that workflow's tests as this campaign. Never bill on arbitrary
PRs, and never silently fetch credentials for a documentation example.

### Deployment

Harness/controller release is source/test automation, not a gateway deploy.
The named operator binds verified target/image, dedicated identities, numeric
token/spend caps, SLO and recovery owner before manual invocation. Report exact
revision privately. No rollout authority for the controller. On rollback keep
the recovery runner compatible with existing journals until they close; do not
delete journals/cache references needed to revoke active credentials. Cleanup
is mandatory after success, abort and kill and has its own finite deadline.

### Validation

Keep the existing loopback tests, add multi-worker fake-provider/controller
integration with fake time, and fault tests at every partial fixture mutation.
Kill controller/worker processes, lose heartbeat/acknowledgement, restart the
janitor, change an unowned quota concurrently and verify preservation. Inject
missing required stages, five-in-50 errors, slow successful p95, OOM/restart and
telemetry loss; nonzero outcome and simultaneous admissions stop are required.
Actual cloud cleanup cannot be proved by mocked tests alone.

Implement the [R1–R3 fault matrix](campaigns.md#accounting-and-dispatch-fault-matrix)
with #7031/#7032/#7033 fixtures, retaining real-route assertions and exact provider
invocation counts. Report each as proposed/not-run until executed. Distinguish
unknown accounting from fixture cleanup: disabling users or restoring quotas
cannot remove a durable hold. Budget policy exceptions are excluded, including human write-offs and automatic
janitor release; unresolved accounting stays recorded after fixture cleanup. Every live
fault cell remains inside declared campaign limits and uses owned isolated scopes.

| Existing ID | Required result / evidence | Phase and owner |
|---|---|---|
| AC-01 | Smallest checkpoint does real inference through both APIs; records revision, reached stages, failures/cancellations and private evidence; runnable documented command. | First checkpoint, named executor and operator. |
| AC-02 | Incomplete HTTP-200, error bursts, OOM/restarts and skipped stages fail/abort correctly, drain boundedly and cannot claim a passing 200 stage. | Before review, named executor. |
| AC-03 | Partial setup and active-run interruption recover via durable ownership record; quotas/users/secrets/health restored, unrelated resources preserved. Crash tests plus real cleanup receipt. | Before review and after deployment, named executor/live owner. |
| AC-04 | Representative matrix and qualifying ramps satisfy ≥99%, no restarts/OOM, predeclared SLO, distinct human/agent counts and honest blocked cells. | Final live acceptance, named live owner. |

## Approval-to-issue incorporation procedure

After explicit approval, the coordinator/design owner records the approved full
commit SHA and approval-comment permalink. Update each existing issue's Design
section with its verified entry points, approved decision/config/data/error/
recovery contract, dependencies and shared-file owner; link the immutable epic
revision and approval record. Keep the issue's plain-language opening,
Description, Impact analysis, Deployment and Validation sections, acceptance IDs
and live completion boundary. Summarize enough implementation detail in the body
to execute without decoding an unexplained reference; link long procedures.
Keep bodies within the maintained issue-authoring size guidance.

Quote the explicit conservative AC-02 correction beside its retained ID when
publishing the design. It corrects the assistant-authored exact-recovery promise;
it does not authorize an exception or pretend lost provider usage was recovered.
Record unresolved inputs as blocked, not ready. Mark story readiness only when
the named owners and that story's prerequisite evidence exist; maintain the
[epic checklist](README.md#epic-readiness-checklist). No issue updates, approvals,
developer dispatches or deployment actions are performed by this proposal.
