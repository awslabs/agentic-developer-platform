# Proposed campaign, reporting and cleanup contract

Part of the [#7030 design](README.md); approval is tracked at the exact PR commit. None of the campaigns below
has been executed by this review. They are implementation requirements for
[#7036](https://github.com/aws-e/adp/issues/7036), with #7034 owning policy
comparisons and the coordinator naming the live operator. Do not run a live
campaign from these examples before target authority and finite cost limits exist.

## Existing versus proposed interface

The current [Locust entry point](../../../tests/performance/gateway/locustfile.py#L12)
reads one token file, has process-global counters, stage end times and a request
cap, writes private JSONL, and validates streamed content/terminals. It is not a
distributed campaign controller, fixture recovery service or fleet error guard.
The existing [README](../../../tests/performance/gateway/README.md#L1) documents
`PERF_API`, `PERF_MODEL`, prompt/output controls, cache-prefix variation and
`PERF_STAGES`. Preserve those knobs as an explicit legacy single-process mode;
do not silently reinterpret cumulative times as dwell times.

**Proposed**, not currently executable: a controller under
`tests/performance/gateway/` accepts `--config`, `--validate-only`, `--run`,
`--resume-cleanup` and `--report`. Configuration errors, missing required targets,
unknown model/API mappings, unsupported versions or absent limits fail before
fixture mutation or inference. No default live target/model/credential; examples
use synthetic names, not real operator paths. CLI inputs identify protected
configuration; credential bytes never appear in arguments, console output or
public artifacts. The controller starts Locust with server-side attempt permits;
it does not multiply `PERF_MAX_REQUESTS` by worker count.

| Versioned configuration field | Required semantics |
|---|---|
| `schema_version`, scenario, seed | Versioned parser/report schema and reproducible prompt/identity selection. Reject unsupported versions. |
| Private target and credential references | Operator-selected environment/model bindings, approved account/role and exact source/image/config evidence retained privately. No ambient credential fallback. |
| Identity set | Owned human/service identities, tenant membership and root-human attribution; per-identity private credential handle and fixture ownership receipt. |
| Model/API entries | Explicit Opus 5.5 and GPT-6 Astra bindings; record compatible Chat/Responses path, SDK version, output/reasoning limits and provider quotas. Test unsupported combinations as not-applicable with reason, not fake successes. |
| Stage schedule | Desired sessions, spawn rate, minimum full dwell, allowed fan-out, think time and required/not-required flag. |
| Global bounds | Required request/physical-attempt cap, active seconds, drain seconds, input/output token reservations, maximum priced spend, setup/cleanup deadlines. |
| Success SLO | Predeclared p95 first-content, HTTP lifetime and terminal/handoff delay by workload class; at least 99% complete streams plus dependency/health criteria. No retrospective thresholds. |
| Stop guard | Fixed parent defaults; only an explicit coordinator record can substitute another finite plan. Missing health telemetry aborts qualification. |
| Private evidence retention | Protected destination, encryption/access control, owned recovery journal, deletion/retention rules. No public raw upload. |

Maximum spend must be numeric and operator-approved. Reserve conservative input,
reasoning and output cost before every physical attempt using the selected route's
quote contract. Unknown price, missing cap or insufficient budget means no
admission. Track actual spend plus outstanding reservations; uncertain failed
attempts retain their upper bounds, not zero. Use provider token/cache evidence
for measured cost. Unique-prefix prompts are only a cache-miss experiment until
usage counters confirm it. Do not publish token values or real prompts.

## Finite plans and implementation checkpoints

Each **model campaign**, including warmup, controls and retries, is capped at
12,000 physical inference attempts, 1,440 seconds of active runtime and 240
seconds of drain. Request/time limits are conjunctive: stop on whichever comes
first. The safety guard can stop much earlier. Reaching a budget before a
mandatory stage finishes is incomplete, not a pass. Do not restart an aborted
campaign under a new label to evade the bound. Additional campaigns need a
declared matrix and aggregate token/spend authorization before execution.

| Plan | Finite candidate schedule / caps | Purpose and gate |
|---|---|---|
| Discovery, each model | ≤300 attempts, ≤300 active seconds, ≤120s drain; concurrency 1 → 5 → 10. Include direct controls inside those limits. | #7031/#7033 diagnoses; fault-injection work uses local controlled services first. No load escalation to 200 while failures remain. |
| Smallest live harness checkpoint | ≤40 attempts, ≤180 active seconds, ≤120s drain; both actual gateway APIs at concurrency 1–2 using compatible models. | #7036 AC-01: verify actual inference, records, stop/drain and cleanup before larger matrices. These are separate counters per model plus one global cap. |
| Qualifying model ramp | 10 / 60 / 100 / 150 / 200 sessions, dwell 60 / 120 / 120 / 120 / 300 seconds **after target users are reached**; transitions ≤120s combined. ≤12,000 attempts, ≤1,440 active seconds, ≤240s drain. | Separate Opus and Astra campaigns; #7031/#7032/#7033 fixes and diagnostics ready. At least five full minutes at 200, never an extrapolated plateau. |
| Identity/cache/fan-out comparison | Each cell ≤1,200 attempts, ≤600 active seconds, ≤240s drain; at or below already demonstrated safe in-flight capacity. | Compare shared identity, distinct users same tenant, distinct tenants; cached/unique contexts; developer-only versus explicit agent fan-out. No cell can silently expand cost authority. |
| Bounded longer soak | Candidate 20 minutes at the already qualified concurrency, ≤12,000 attempts, ≤1,440 active seconds including warmup, ≤240s drain. | Longer than the five-minute plateau, not proof of indefinite sustainability. Must declare its own numeric token/spend budget before approval. Stop if attempt bound prevents the required 20 minutes; report incomplete. |
| Burst and recovery | Idle two-pod baseline → previously qualified burst ceiling for 180s, then zero admissions and normal cooldown; ≤2,000 attempts, ≤600 active seconds, ≤240s stream drain. | Attribute startup lag and controlled overload. Cooldown observation has a separate finite 600s deadline with no inference admissions. |
| Mixed models | ≤2,000 total attempts, ≤600 active seconds, ≤240s drain, explicit model proportions and individual cost counters. | Interference characterization after single-model qualification; not a replacement for either required full ramp. |

Requester's latency SLO remains **unresolved**. The 90s guard is an emergency
stop, not an acceptable latency target. The D2 handoff/EOF and backlog thresholds
are proposal values to test and approve; they cannot retroactively define a
qualifying campaign as successful.

Distinct identities and fan-out must be measured, not inferred. Report:
`configured_sessions`, `live_sessions`, distinct human/service identities,
distinct tenants, human-rooted chains, fan-out distribution, current/peak/time-
weighted in-flight generations, starts/completions per second, and provider
attempts. A session with think time can have no request in flight; 200 sessions
does not require or establish 200 continuously active HTTP requests. Conversely,
agent fan-out can make fewer sessions generate more than 200 requests. Bound
that total explicitly and report achieved concurrency rather than renaming it
“developers.” Use a separate bounded saturation cell if actual simultaneous
request capacity is the intended claim.

## Stop, drain and classification

Stop **new** admissions across every worker when any parent condition occurs:

- At least five failures among the latest 50 completed attempts, once at least
  20 attempts have completed; until 50, use the available completed window.
- Successful rolling p95 first-content latency exceeds 90 seconds. Proposed
  window: last 50 successes, minimum 20; report insufficient samples, not zero.
- Any gateway OOM or restart, including a restart unrelated to the load while
  qualifying. Classify its cause separately; do not ignore it for certification.
- Request/time/token/spend bound, lost controller lease, lost mandatory telemetry,
  cleanup/ownership integrity failure or operator abort.

Workers require short-lived controller permits; no controller or expired permit
means no new request. Reserve all possible SDK attempts before dispatch, and
prefer single-attempt provider config for qualification. Count connect failures,
admission failures, timeouts, provider retries and cancellations, not merely
successful response bodies. Request accounting and provider-attempt accounting
are distinct counters; both are capped. Retries do not reset a request deadline.

Drain active streams concurrently for at most 240s. Close/cancel remaining clients,
classify each outstanding attempt, and record unknown outcome after runner loss.
Do not wait 240s once per user. Kubernetes stream grace may be 960s; that does not
extend the campaign drain budget or authorize an inference replay. Provider work
may outlive a dead client, so outstanding reservations stay counted until trusted
reconciliation. Observe baseline recovery on its separate bounded cleanup clock.

Current `STREAM TTFT/*` timing rows are not additional generations. Use one
attempt record per generation/physical retry identity and avoid Locust aggregate
double counting. Record API headers status and semantic outcome separately.

| Outcome | Success? | Accounting/report action |
|---|---|---|
| Content + valid completed terminal + clean EOF | Yes, unless another stream error occurred | Include in successful latency percentiles and all-attempt denominator. |
| HTTP 200, empty/partial stream or missing terminal | No | Identify before-content versus after-content, observed usage and connection exception category. |
| `response.failed`, `response.incomplete`, explicit error | No | Preserve terminal subtype, including output/reasoning-budget exhaustion; no fake completion. |
| EOF without actual provider terminal | No | Translator regression test must prove no synthesized success marker. |
| Timeout, client cancel, drain cancel, lost worker | No | Keep in denominator; unknown completion/usage is explicit, not discarded. |
| Admission/authorization/quota rejection | No for capacity | Classify separately; normal denied requests in negative security tests are expected, not included in the qualifying traffic population. |

Report all-attempt outcome counts, missing/unknown records and failure durations
alongside successful p50/p95/p99 first-content, provider-terminal, durable-handoff,
terminal-to-EOF and full HTTP lifetimes. Never manufacture a first-content sample
for an empty response. Latency censored by cancellation is labelled censored.
Assign an attempt to its admission stage; retain its final outcome after stage
end, and explicitly count carry-over concurrency. A stage report cannot pass
until its admissions reconcile with terminal/failure/unknown outcomes.

## Accounting and dispatch fault matrix

These are **required implementation tests, not executed results**. They implement
the [revised authority/dispatch contract](accounting.md); #7036 owns orchestration,
#7031 PostgreSQL contention fixtures, #7032 accounting fixtures and #7033 transport
faults. Exercise actual authenticated Chat/Responses routes with PostgreSQL and
Redis, controlled provider/S3 substitutes and independent worker processes, not
only mocked helper calls. Scope-specific refusal is an expected negative-test
outcome, never a passing capacity request. Keep all existing acceptance IDs.

| Case | Action and decisive expected result | Existing acceptance mapping |
|---|---|---|
| F01 concurrent headroom | Barrier-synchronize admissions with combined bounds exceeding a shared user/org cap. Exactly the affordable set commits holds; denied calls make zero provider invocations. Repeat concurrent settlement and duplicate server keys. | #7031 AC-01/02; #7032 AC-02 |
| F02 Redis flush/TTL/rollover | Kill Redis or expire keys, roll daily/monthly periods, freeze recovery and race new admissions while SQL attempts remain. No lost bound/unknown barrier; all cache rebuilds are versioned. Strict uninitialized policy scopes refuse, never initialize empty. | #7031 AC-02; #7032 AC-02 |
| F03 process-kill boundaries | Kill before/after SQL prepare, S3 admission acknowledgement, arm commit and provider submission. Only never-armed/fenced or proven-unsent attempts release; armed ambiguity persists, expires into a SQL-visible barrier and never replays inference. | #7032 AC-01/02; #7033 AC-01 |
| F04 isolation and cross-org person | Same organization/different users share the org barrier; same fused person in two organizations shares person exposure; another person in a disjoint organization remains admissible. Link/unlink aliases, membership/cap changes and observe/off cannot hide an old hold. Source failures deny affected resolution; no cross-tenant error details. | #7031 AC-02; #7032 AC-02; epic AC-03 |
| F05 unknown window | Kill after provider completion/usage observation but before durable final receipt. Retain possible spend, classify unknown, block affected new spend, and publish **no exact recovery pass**. No charge guessed from bound/transcript and no zero-cost claim. | #7032 corrected AC-02: prove containment, not exact recovery |
| F06 durable receipt replay | Kill after S3 acknowledgement and before/after SQL commit, lose commit reply/Redis update, duplicate and reorder events. Exactly one receipt/debit/usage row and atomic hold removal; immutable historical price/scope; corrupted receipt quarantines. | #7032 AC-02 |
| F07 late receipt and reconciliation | Race trusted receipts with duplicate/stale evidence-backed reconciliation. Exactly one historical debit and hold removal; stale revision returns 409. A receipt contradicting proven-unsubmitted disposition quarantines/reblocks rather than silently overwriting history. | #7032 AC-02; #7035 AC-02 |
| F08 forbidden release | Ordinary caller, service principal and org admin try cross-org release; bad evidence, absent logs, TTL, disabled identity and lost runner try clearing holds. Refuse, no debit or state mutation. Financial exception actions are unsupported and must reject without mutation, including for a platform admin. | #7032 AC-02; #7036 AC-03 |
| F09 quote expiry in journal I/O | Delay S3 admission write beyond quote expiry while unrelated requests run. Final guard refuses; provider call count zero. Only the correct unsent attempt releases, within original deadline; no internal re-quote/replay. | #7032 AC-01/02; #7033 AC-01 |
| F10 generation/policy race | Publish a new generation or policy revision during S3/SQL/transport wait. Old policy cannot arm; newly visible pricing generation cannot send. Order publication immediately before/after handoff; only the already-dispatched invocation may use the historical snapshot. | #7032 AC-02; #7033 AC-01 |
| F11 final transport queue | Delay executor, credentials, connection acquisition and adapter confirmation separately. A permit expiring there never invokes provider. Kill at the one-use handoff or lose send acknowledgement: unknown, no release/replay. Assert no SQL transaction remains open over any wait. | #7031 AC-02; #7032 AC-01/02; #7033 AC-01 |
| F12 recovery/cache fencing | Race two recovery workers, Redis snapshot publication and new admissions; kill claimant, resume old epoch, drop notifications. Stale writers cannot arm/clear/settle conflicting state. SQL index finds every open attempt; scanner delay cannot bypass barriers. | #7032 AC-02; #7036 AC-03 |
| F13 capacity and diagnostics | Compare authority enabled/disabled under identical safe load, recording SQL operations/attempt, lock/pool wait, journal/guard latency, recovery and exporter failure. No metric outage changes admission or charges. Include added connections in rollout overlap and scale-out tests. | #7034 AC-01/02; #7035 AC-03; epic AC-04 |
| F14 migration/rollback | Mix old/new readers before activation; refuse activation with old ungated writers or unresolved legacy exposure. Restart/rollback retains tables/aliases/audits, stops affected spend rather than bypassing active gates. | #7032 AC-02; #7034 AC-02; #7036 AC-03 |

Record per-test state transitions, affected/disjoint admission outcomes, physical
provider-call count, receipt/debit/usage-row counts, bounded cleanup, and SQL
transaction lifetimes in protected evidence; publish only sanitized aggregates.
Every fault suite has finite wall-clock/operation limits and tears down only its
owned fixtures. Run locally first with no billed traffic. Live confirmation uses
the existing discovery cap (300 attempts, 300 active seconds, 120s drain per model)
or a separately approved finite plan, never the qualifying 200-session run for
first-time destructive faults. Retries count toward the same bounds. F05 can pass the corrected containment subclaim while reporting usage as unknown;
it cannot report exact recovery. A qualifying capacity campaign still fails if
required accounting remains unresolved.

## Fixture lifecycle and recovery after runner loss

The controller's protected recovery journal exists **before** the first mutation
and is remotely durable, not only a temporary file on the test runner. Each
mutation has a unique campaign owner, exact resource binding, before value,
intended after value, observed result/version and recovery action. No plaintext
credentials in that journal. Record intent first, reconcile ambiguous outcomes
by reading the exact resource, and retry idempotently. No tenant enumeration or
broad cleanup by a name prefix alone.

1. Verify authorized private target/account/exact role, baseline health and model
   access. Read existing deployment and quota state without claiming that source
   commit equals deployed image. Reject unowned/shared identities as fixtures.
2. Create/enable dedicated owned identities and scoped quotas only. Normal-user
   limits remain unchanged. Record every grant and preserve original values;
   compare-and-swap restore avoids overwriting an operator's concurrent change.
3. Write tokens to owner-only temporary storage; use broker/private references
   in configuration. Validate memberships and root-person attribution through
   supported interfaces, never by crafting trusted headers.
4. Heartbeat the campaign lease and checkpoint admissions/counters. A separately
   authorized recovery runner/scheduled janitor watches expired leases and can
   complete cleanup without the original process. #7036 must implement and test
   that runner; a `finally` block or GitHub `always()` step alone is insufficient.
5. On success, guard abort, signal, runner kill or setup failure: stop permits,
   drain/cancel, reconcile outstanding inference/accounting, restore owned quotas,
   revoke/sign out/disable temporary identities, and remove cached credentials.
   Existing unowned resources are never deleted or reset.
6. Verify restored quota versions, identity revocation, no remaining owned active
   clients, retained accounting evidence, normal-user denial checks and baseline
   health. Recovery failures produce retryable records and a nonzero status,
   escalating to the named operator after the finite deadline. Keep protected
   evidence; do not declare cleanup complete merely because the worker died.

Fixture cleanup must not delete SQL metering attempts, scoped barriers, immutable
receipts or reconciliation audit events. If usage remains unknown, retain the
accounting incident and its scope block after fixture shutdown; report cleanup
and accounting status separately. A campaign janitor has no financial exception
authority. Only the [authorized reconciliation contract](accounting.md#proposed-authorized-reconciliation-interface)
can dispose of a hold using trusted receipt or proven-unsubmitted evidence;
no risk-exception action exists in this scope.

Proposed setup and cleanup deadlines: 600 seconds each, at most three attempts per
idempotent mutation, with jitter bounded by the overall deadline. The janitor
lease interval and clock-skew tolerance must be explicit in #7036; stale owner
fencing must prevent two recoverers mutating fixtures concurrently. A machine
without authorized recovery credentials cannot promise cleanup: refuse fixture
creation unless a verified recovery owner exists. No credential substitution or
deployment mutation is implied by fixture recovery authority.

## Acceptance and reporting

Report schema contains source/config hashes, stage schedule/actual dwell,
attempt reconciliation, all outcomes and latencies, resource/dependency series,
identity/cache/fan-out matrix, stop reason, fixture recovery result and each AC
status (`pass`, `fail`, `blocked`, `not-run`). Exact deployment/identity/correlation
bindings stay in protected evidence. Public Markdown contains sanitized summary
statistics only. Raw JSONL contains provider error details today; do not upload
it publicly even if bearer headers were omitted.

Exit behavior: 0 only when every mandatory scenario/AC and cleanup passes;
nonzero distinct categories for invalid configuration, failed/aborted campaign,
incomplete stage/budget exhaustion, and incomplete cleanup. A report command
cannot turn `not-run` into a passing zero exit. Local parser tests use loopback
synthetic events; only protected manual automation obtains live credentials.

| Epic ID | Required evidence / linked story ACs | Current status |
|---|---|---|
| AC-01 | Every #7031/#7032/#7033 defect AC, plus diagnostic/automation regressions, mapped to deployed revision and protected receipts; fixes reviewed and live failure cases repeated. | Not-run; runtime causes and implementation remain to be verified. |
| AC-02 | Both complete 10/60/100/150/200 ramps, five full minutes at 200, ≥99% content-bearing terminally complete streams at each required plateau and overall, no restart/OOM or cascading dependency failure; #7031 AC-03, #7032 AC-03, #7033 AC-03, #7036 AC-04. | Not-run; supplied baseline fails target. |
| AC-03 | Separate identities/tenants, cache counters, explicit fan-out, bounded longer soak/burst and honest mixed-model matrix; #7034 AC-02 and #7036 AC-04. | Not-run; matrix inputs and costs require operator plan. |
| AC-04 | Predeclared latency SLO plus final floor/requests/limits/triggers/ceilings/headroom/connection budget, provider versus gateway timing and startup breakdown; #7034 AC-01–03, #7035 AC-01–03. | Blocked on SLO selection and qualifying measurements. |
| AC-05 | Parser/controller false-success regressions, success/abort/runner-loss cleanup receipts and baseline health; #7036 AC-01–03. | Not-run; automation is proposed. |

The 99% threshold is a success criterion, whereas five failures in 50 is a safety
stop. A run may avoid the guard and still fail acceptance. Low sample counts and
uncertainty intervals must remain visible; a short zero-error test is not proof
of a universal reliability rate. Mandatory blocked/not-run cells keep the epic
and corresponding child open.
