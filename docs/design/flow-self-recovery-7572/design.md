# Flow self-recovery: design for issue 7572

## Verdict and scope

**Ready for design review; not blanket implementation or deployment approval.**
The nine stories should remove routine operator intervention while preserving
accepted spending, retry, tenant and deployment boundaries. Most recovery work
can proceed independently under the contracts below. The platform owner must
resolve the deployment permission decisions in [7457](#7457-deployment-authority)
and [7513](#7513-tools-deployment) before those permission changes are implemented.
No live acceptance is claimed.

This document is the repository deliverable for
[sub-epic 7572](https://github.com/aws-e/adp/issues/7572), beneath
[autonomous delivery 4191](https://github.com/aws-e/adp/issues/4191).
It does not implement the engine, change an issue's accepted contract, dispatch
workers, modify a live environment or authorize deployment. The child-body
amendment is addressed by reviewable per-child Design/Deployment material here;
it is **proposed**, not described as owner-approved or silently written into the
nine issues. The existing native issue hierarchy and all acceptance/scenario
identifiers remain authoritative. The delivery coordinator can apply approved
refinements after this PR is reviewed, without replacing native parent links.

### My understanding of the task

Recover failures that need no new human decision: workers that never start,
vanished assignments, temporary budget holds, lost settlement, compatible plan
amendments, human evaluation gates and missing CI failure detail. Let owners put
inactive flows away. Complete a deployment model in which an accepted bounded
plan and reviewed release can authorize ordinary delivery without a human
editing each release SHA. Make the tools pipelines usable without giving their
release identity unrestricted infrastructure authority.

### How I plan to implement it

The implementation backlog reuses the existing durable execution/action ledger
for scheduling and duplicate suppression, the existing protected worker authority
for fencing old assignments, and the existing accounting receipts for settlement.
Each story has an independently testable boundary. Shared-file integration is
sequenced without imposing unnecessary runtime dependencies. Local tests precede
component rollout; live observations and the epic's 24-hour window follow it.
This PR completes design and static inventory only.

## Evidence boundary and corrections to the issue text

Source baseline: `849f6163e59855c1554bc263e9ad96695730e9bd`, fetched `origin/main`
on 2026-10-13. The task branch began at an empty worker checkpoint above this
revision. Issue bodies and comments were fetched during this review; their
historical operator statements are reported evidence, not independently repeated
live observations. No private logs, flow identifiers, account identities or
environment addresses are reproduced here.

| Supplied assumption | Current source evidence and consequence |
|---|---|
| Reviewer retries consume `node.attempts` | [Stage allowance accounting][stage-attempts] counts durable actions independently per stage. Exempt only proven startup failures in this accounting; do not decrement node history. |
| Missing SQL worker receipt proves protected bootstrap failure | [Review dispatch][review-dispatch] reads protected EXEC records; [startup classification][startup-recovery] and [bootstrap refusal][bootstrap-failure] are narrower than a missing receipt. Absence alone cannot release authority or spend. |
| Human evaluations always take generic dispatch | [Managed evaluation][evaluation-plan] already includes accepted evaluation specifications with execution policy; [controller][evaluation-controller] already has a human gate branch. Finish the uncovered/default path and test current behavior before rewriting it. |
| Component selection is still hard-coded Python | [Path selection][deployment-workflows] already uses ordered manifest rules. Remaining work includes coverage, release-derived revisions and authority, not replacing an if-chain. |
| Tools role does not exist in source | [Tools role][tools-role] already exists, default-off. Its IAM mutation and namespace-wide edit permissions conflict with the story's narrower contract. Source presence proves neither applied identity nor permission correctness. |
| All tools workflow admission is absent | [Trust inventory][trust-inventory] includes façade/platform, but not interceptor at the audited revision. Add only the missing admission after trust validation. |
| Every change automatically deploys through the gateway workflow | [Deployment inventory](inventory.md#delivery-paths) distinguishes gateway, frontend, hosted worker, manual infrastructure and tools workflows. Building an image is not updating an existing worker. |
| Public non-secret target identities belong in source | [Publishing policy](../../PUBLISHING.md) prohibits real account/resource/operator identities even when not secret. Keep portable input schemas/defaults in source and target bindings in protected configuration. |

The reusable lineage family is [7536](https://github.com/aws-e/adp/pull/7536),
[7540](https://github.com/aws-e/adp/pull/7540),
[7548](https://github.com/aws-e/adp/pull/7548),
[7549](https://github.com/aws-e/adp/pull/7549),
[7556](https://github.com/aws-e/adp/pull/7556) and
[7570](https://github.com/aws-e/adp/pull/7570).
Accounting reuses [7451](https://github.com/aws-e/adp/issues/7451),
[7520](https://github.com/aws-e/adp/pull/7520) and
[7567](https://github.com/aws-e/adp/pull/7567), verified in the referenced source,
not inferred from closed issue status.

## Shared architecture and invariants

```text
accepted plan + human supplements
             |
      admission / current authority
             |
 SQL node + execution + action reservation ----> protected worker EXEC
             |                                      |
     scheduled observation <---- trusted startup / terminal / PR evidence
             |
 retry at durable due time OR block with a typed, actionable reason
             |
 reviewed merge -> target lease -> correlated workflow -> runtime verification
             |
 evaluation evidence -> retained human gate OR authorized machine acceptance
```

1. **No inference of success from silence.** Missing evidence can justify a
   bounded, fenced recovery only. A disappeared process is not a completed model
   request, failed review, released target lease or refunded budget.
2. **One durable action per effect.** Prepare the operation under the existing
   execution identity and ownership generation before dispatch. On timeout,
   reconcile that operation before creating another. Restart/duplicate ticks
   must not reset counters, due times, policy expiry or accepted allowances.
3. **Current authority remains mandatory.** Use
   `plan_lineage.execution_plan_matches` for execution version comparisons;
   `receipt_plan` verifies the receipt's original hash and accepted history.
   Never replace these with `old_version <= current_version`. Reconcile via
   `flow_meter.reconcile_flow_meter` before policy meter reads. Preserve the
   abandoned-marker conservative settlement rather than releasing unknown spend.
4. **Tenant and actor binding.** New contracts use `tenant_id`; legacy SQL
   `org_id` remains its storage synonym. Existing personal/org identifiers do
   not change. Validate flow, node, attempt, claim generation and tenant together.
   Cross-tenant reads return the existing nondisclosing not-found behavior.
5. **Human routes are escape hatches, not a scheduler.** Resume,
   resume-continuation, retry and budget remain available with their existing
   permissions, optimistic snapshots and audit decisions. Recovery never grants
   additional spending or deployment scope.
6. **Code, deployment and acceptance are separate.** The parent requires ordinary
   code stories to conclude on verified merge, while declared feature/live gates
   remain separate. These nine defect contracts explicitly include live checks;
   neither this design PR nor a child merge closes the epic.

### Storage fit and compatibility

[The migration inventory](gateway-migrations.tsv) enumerates every gateway
migration, including merge revisions; it is not a maximum-filename shortcut.
Existing [SQL models][models] own graph/decision/action/lease state.
[Budget settlement receipts][budget-receipt] already key terminal debits by
`org_id, request_id` and bind trusted reservation scopes. Redis reservations
retain conservative bounds; [protected authority storage][authority-table] uses
`pk, sk`, with tenant-prefixed EXEC keys retained for audit; grant expiry is
enforced explicitly, not by deleting authority records through TTL. Webhook-event
status is a separate diagnostic record, not equivalent to a worker completion.

Only archiving requires a new SQL column in this design. Startup/retry metadata
uses versioned action `detail` and append-only decisions, not a speculative
execution metadata column. No new DynamoDB table, S3 bucket, secret path or
credential service is proposed. Readers must accept old records conservatively:
unattested historical failures continue to count; old receipts must pass their
existing validation; old manifests remain readable under their old rules.

## Per-child design contracts

### 7559 Reviewer bootstrap accounting

**Outcome:** a reviewer that provably never received usable model authority does
not consume a reasoning attempt, and repeated startup failures stop with the
real cause. Scope remains review continuations; do not silently generalize the
refund to developer runs or reviews that actually executed.

| Starting point and reuse | Change at the audited revision |
|---|---|
| [Review dispatch][review-dispatch], [review cycle][review-cycle] | Reuse protected EXEC lookup, operation keys, scheduling and continuation preparation. |
| [Stage counts][stage-attempts] | Count reserved/unknown actions; exclude only a validated startup-exempt action. Apply the same rule to aggregate display and authorization. |
| [Startup recovery][startup-recovery], [bootstrap refusal][bootstrap-failure] | Reuse gateway-attested refusal and fenced unstarted-continuation classification, not an untrusted exception string. |
| [Worker bootstrap status][worker-bootstrap] | Diagnostic failure is currently written to webhook activity; do not claim it is a SQL terminal receipt. |

**Implementation contract.** Extend action detail with a versioned startup
classification: original invocation/attempt, failure category, bounded sanitized
step/detail, trusted evidence reference, observed time, exemption status and
failure ordinal. Only the gateway's trusted refusal/fencing path may certify
`bootstrap_exempt`; absence of a worker receipt by itself is insufficient.
The existing refusal classifier can describe a workload-bound record before
credential issuance, so requiring “no workload binding” would miss valid cases.
Unknown, issued-authority or contradictory evidence keeps the reserved attempt
charged and blocks automatic exemption. Keep action/EXEC records intact.

Derive the per-cycle streak from durable actions/decisions, not process memory.
Default `max_bootstrap_failures_per_cycle=3`; after failure one retry at +60s,
after failure two at +300s; failure three blocks `review_bootstrap_failing` and
does not dispatch a fourth. This deliberately resolves the issue's contradictory
“1m, 5m, 15m” schedule versus three-dispatch limit: 15m is a ceiling for a future
higher configured limit, not permission for a fourth default dispatch. Changing
error fingerprints does not evade the three-failure bound. Record a normalized
fingerprint for diagnosis; success beyond bootstrap resets the streak.

Reuse the action scheduler's `next_check_at`, with operation identity including
the new invocation. A retry first fences prior authority and rechecks budget,
policy, claim and review-stage allowance. A verified bootstrap exemption refunds
only attempt classification, never a financial debit. Authorized resume routes
append a reset decision and schedule at most one new action; they do not erase
history, create fresh budget or reset model-executed attempts. Show cause and due
time through the existing execution block/progress API and node panel.

**Prerequisites/owner:** engine developer implements the attested classifier;
worker owner adds structured startup reporting only for categories not already
attested. New categories must fail closed until both producer and consumer are
released. General CloudWatch parsing is not an authority source.

**Deployment/recovery:** gateway backend/tick first; hosted worker release only
if reporting changes. Older consumers count all attempts safely, so rolling back
does not erase history but can exhaust the old allowance sooner. Operator may
resume after fixing the platform cause; no SQL editing. Existing AC-01..03 and
SCN-01..03 remain, with negative/duplicate/restart cases in the backlog.

### 7563 Vanished developer recovery

**Outcome:** retry one demonstrably unstarted, fenced protected assignment within
accepted limits; detect it early instead of waiting for the long-running pod
deadline. A worker that previously acquired workload/model authority but then
vanished is **not** proven unstarted by a missing SQL receipt.

| Starting point and reuse | Change |
|---|---|
| [Developer recovery][developer-recovery] | Today accepts the trusted results observer's failure decision only. Add a distinct validated stall origin, without weakening normal terminal checks. |
| [Stall detector][stall] | Preserve long-run timing and continuation clocks; add a protected unacknowledged deadline. |
| [Protected startup recovery][startup-recovery], [authority table][authority-table] | Reuse fencing and authoritative attempt identity; SQL absence is not an EXEC read. |

**Implementation contract.** Proposed `ORCH_NEVER_ACKNOWLEDGED_SECONDS=1200`,
positive integer strictly below the normal stall deadline; invalid values fail
configuration validation. Anchor it to the committed dispatch/current assignment,
not the original story start, and respect flow pause/resume as current timing does.
Use an injected clock. Before early failure, strongly read the protected EXEC and
verify the exact tenant/node/attempt/claim: no workload binding, no issued startup
authority, no registration or terminal outcome. Missing/unreadable EXEC is
`worker_exit_unverified`, not proof that an old worker cannot wake up.

Conditionally fence/revoke the prior assignment using existing authority and
claim mechanisms **before** preparing replacement work; re-read on races. A late
bootstrap loses the conditional claim and cannot execute. Persist the fence
receipt and `NODE_STALLED` decision with assignment identity. Recovery recognizes
only the trusted stall service origin with that receipt, rechecks accepted plan
lineage and reconciled budget, and uses existing developer retry backoff
`min(60 * 2**min(attempt-1,5),1800)` seconds. It records `stall_retry`, returns the
node to `ready`, and consumes the next attempt only on actual admission/dispatch.

Allow one automatic stall replacement per node's recovery lineage, durable
across plan amendments, restarts and normal ticks. A second unacknowledged stall
requires the operator with “worker vanished twice”; contradictory evidence or
lost ownership also blocks. Accepted allowance exhaustion remains authoritative.
The stale-run reservation follows normal conservative accounting, not a refund.
No new schema or automatic attempt reset. Later manual recovery keeps history.

**Prerequisites/owner:** engine developer owns conditional fencing and race
tests. The historical pod disappearance cause is unverified and does not block
this bounded recovery; a bound running worker remains outside this story's
automatic-retry case until trustworthy exit/fence evidence exists.

**Deployment/recovery:** gateway backend/tick; no new role or worker protocol is
required for the initial unbound case. Revert the recovery consumer without
reopening fenced attempts. Existing manual resume remains. Preserve AC-01..03,
SCN-01..03; clarify AC-01's missing-receipt fixture with the stronger fence proof.

### 7571 Durable settlement and budget waits

**Outcome:** do not acknowledge a successfully completed stream until its trusted
usage is durable; keep budget-hold throttling distinct from provider throttling.
Graceful shutdown drain is useful but cannot guarantee anything after SIGKILL.

| Starting point and reuse | Change |
|---|---|
| [Responses/Mantle passthrough][mantle-stream], [proxy finalization][proxy-service], [route capture][proxy-routes], [app lifespan][lifespan] | Mantle already retains `_usage_finalizers` but yields upstream terminal bytes before its finalizer. Reuse/extend that registry and order terminal delivery after durable settlement; an in-memory task is not durability. |
| [Usage persistence][usage-service], [settlement receipt][budget-receipt] | Reuse idempotent `(tenant, request)` settlement and trusted scope attestation; no new outbox/table is necessary for the receipt-first choice. |
| [Reservations][reservations], [flow reconciliation][flow-meter] | Keep expired-marker upper-bound settlement; trusted receipts can reconcile still-unresolved live markers, not automatically lower an already-abandoned bound. |
| [Shared worker proxy][sigv4-proxy], [worker config][codex-config] | Intercept only pre-provider `budget_reservations_pending` responses at the common gateway transport, before SDK retry exhaustion. |

**Implementation contract.** Treat a provider's terminal usage event as an input,
not yet a client-visible completion. Capture usage and pricing; write/confirm the
durable trusted receipt through the current accounting transaction; then emit
the successful terminal event/HTTP completion. Cover Responses as well as Claude
compatibility routes, streaming and non-streaming. In Mantle's currently
byte-preserving passthrough, buffer at most one bounded SSE event so a terminal
event cannot escape before its usage commits; do not buffer the whole response
or alter nonterminal payloads. Preserve malformed/oversized-event refusal and
never inject an error inside a partially forwarded event. Internal “provider complete”
logs must not imply “client completion settled.” The proxy's existing catch-and-
log usage failure must become an explicit unsuccessful settlement outcome at
this boundary. If no durable receipt can be confirmed, do not emit a successful
completion; retain the conservative reservation and expose proposed error
`usage_settlement_unconfirmed` with `retryable=false` for the model operation.
Accounting reconciliation may retry by the same receipt key; model transports
must not retry the already-executed provider call to fix accounting. The worker
integration must verify that this distinction survives SDK error translation.

Reuse the existing Mantle finalizer set and register other pending finalization
tasks before their first await. Lifespan stops new
admissions, drains them within a timeout shorter than Kubernetes termination
grace, then closes shared clients. A database commit whose response is lost is
resolved by the existing receipt key, never a second debit. Kill before durable
write means an incomplete client stream with an abandoned bound, not fabricated
zero usage; kill after write is repaired by `reconcile_flow_meter`. This trades
some tail latency/availability during a database outage for truthful completion.

The shared proxy buffers only bounded non-2xx response bodies before forwarding
headers. For HTTP 429 with exact gateway error `budget_reservations_pending`,
retry the same not-yet-admitted request with capped exponential backoff and
bounded jitter, respecting valid `Retry-After`. Re-sign each request and refresh
authority through the existing credential path; cancellation aborts sleep and
I/O. Never buffer a successful stream or replay after streaming begins. Provider
429, ordinary rate limiting, 402 and 503 keep existing semantics. Emit bounded
“waiting for budget holds” progress and `budget_wait_exhausted` at the deadline.

Proposed `ADP_BUDGET_WAIT_MAX_SECONDS=4200`, bounded additionally by remaining
worker lifetime and accepted policy expiry; invalid/nonpositive values refuse
startup configuration. The issue's 3660-second suggestion is insufficient:
current marker lifetime is **3660s plus 300s abandonment grace** before scheduled
reconciliation. A wait does not reserve a second model call, extend authority or
consume another node attempt. The client retries only a denial known to precede
provider execution. Preserve gateway body/headers so other clients retain their
own retry policy. Add transport tests through the installed worker path rather
than treating an SDK option as proof of behavior.

**Prerequisites/owner:** gateway developer owns completion ordering and receipt
semantics; worker developer owns transport retry and cancellation. Exact old-pod
SIGTERM timing remains unverified, not a design prerequisite. Validate the new
wait timeout against actual proxy/SDK idle timeouts in the worker integration
test; a heartbeat to the model stream must not fabricate model content.

**Deployment/recovery:** gateway first, then `agent-worker-image.yml` promotion
for new jobs; already-running workers do not change image. Retain old reservation
and receipt formats and the #7451 backstop on rollback. Existing AC-01..02 and
SCN-01..02 remain; extend with kill/replay, deadline and non-budget-429 cases.

### 7382 Receipt-preserving plan amendments

**Outcome:** an unrelated plan content amendment preserves previously accepted
budget, retry, window and concurrency supplements; an incompatible amendment
reports precisely which authority was not carried forward.

| Starting point and reuse | Change |
|---|---|
| [Lineage][plan-lineage], [shared retry][shared-retry] | Preserve original receipt hash/signature and private supplement provenance. Add a separate, narrow receipt-preserving hop; do not widen worker authority comparison. |
| [Amendment route][amendment-route], [amendment impact][amendment-impact] | Compute effective before/after values transactionally; expose both preview and committed outcome. |
| `shared_budget.py`, `shared_retry.py`, `shared_window.py`, `shared_concurrency.py` beside the lineage module | Consume the same proof, not four diverging compatibility rules. |

**Implementation contract.** Permit carry only across authenticated human
amendments where tenant, flow, approving policy principal, repository/resource
scope, permitted actions, credential/environment bindings, human gates,
evaluation authority and original base policy limits/expiry are unchanged.
Exclude derived version/hash identifiers from comparison only after proving
the accepted plan/decision chain. Node text/content changes can qualify;
any authority or limit change, including lowering spend or changing repositories,
invalidates carry. No timestamp refresh: a preserved window receipt retains its
original expiry. Verify every bounded hop, source decision, original hash,
superseded predecessor and effective value; malformed/missing history fails closed.

In the same amendment transaction, persist a receipt disposition snapshot in
the amendment decision: source decision IDs, receipt kind, before/after effective
values and reason. Proposed additive response fields `receipts_preserved` and
`receipts_dropped` are arrays defaulting to empty. A dropped item contains
`kind`, `decision_id`, `before`, `after`, `reason`; no credential values. Return
that same disposition on an idempotent amendment replay. Serialize amendments
and supplement acceptance with the flow lock/expected plan snapshot so a racing
increase cannot be silently lost. The impact preview uses identical rules but
is not itself authority. CLI and UI visibly warn about dropped supplements and
display effective values, including JSON output; no automatic reapproval.

**Compatibility/data:** additive response and decision-detail fields, no table
migration. Legacy dependency-preserving and node-preserving lineage still work;
general execution identity checks continue through `execution_plan_matches`.
Legacy clients ignore new fields but server behavior still prevents silent loss
through the retained decision record. Do not copy private attributes by a plain
Pydantic dump/reconstruction that loses accepted supplement provenance.

**Prerequisites/owner:** engine owner confirms this conservative compatibility
rule during design review; developer owns implementation and concurrency tests.
No need for a new permission to carry existing accepted bounds.

**Deployment/recovery:** gateway/tick, frontend workflow and CLI packaging as
applicable. A rollback keeps disposition records; old readers can fail closed
and require explicit supplement reacceptance rather than quietly widen limits.
Preserve AC-01..03, SCN-01..03; test all four receipt kinds, not budget/retry only.

### 7381 Human evaluation gates

**Decision recommended:** gate-first for human acceptance; no automatic evidence
collector and no `evaluate` capability granted just to present a gate. An explicit
collection contract remains separate from accepting results.

| Starting point and reuse | Change |
|---|---|
| [Managed evaluation selection][evaluation-plan], [tick][tick] | Current specified human evaluations can already be managed. Cover human-default/legacy-ready cases without falling to generic machine acceptance. |
| [Evaluation controller][evaluation-controller], existing gate controls | Reuse the existing `awaiting_gate` and human approval transition. |
| [Delivery progress][delivery-progress] | Currently excludes non-story nodes. Add typed eval waiting/refusal projection instead of “queued” forever. |

**Implementation contract.** On a ready human evaluation with passed
predecessors, load the accepted specification and predecessor evidence under
the current tenant/plan identity. Present a durable gate once, binding evidence
references, PR heads/merge revisions, check results, required deployment receipts
and plan version. If the accepted contract is code-only, predecessor merge/check
evidence is sufficient; do not impose a deployment proof it never requested.
If it requires deployment, missing/stale runtime evidence is an explicit wait or
typed refusal, not a gate with implied success. Default human evaluations without
a machine runner/specification must not be forced through machine-runner parsing.

Guard pause/archive/authority/current predecessor checks; transition through the
existing allowed states to `awaiting_gate` without dispatching a worker. Human
approval revalidates the evidence binding and uses existing PLAN_APPROVE control
to pass the node and release successors. Reject/revoke retains existing behavior;
duplicate ticks/approvals produce no duplicate gate or successor dispatch. No
machine-acceptance permissions are relaxed. A changed plan/evidence snapshot
requires a refreshed gate rather than replaying stale approval.

Extend the existing graph progress DTO additively to eval nodes with stage,
typed reason, actionable detail and next check time. Show admission refusal and
missing-evidence states in NodeChip without claiming they are queued work.

**Prerequisites/owner:** epic owner confirms gate-first (already recommended in
the story). Engine developer first reproduces which current accepted/default
fixtures remain broken; avoid reimplementing the branch already shipped.

**Deployment/recovery:** gateway/tick and frontend. No schema/IAM or worker
deployment. Rolling back the reader leaves existing gates queryable; never
auto-approve a pending gate. Preserve AC-01..04 and SCN-01..03, including machine
evaluation non-regression and stale/unauthorized human approval tests.

### 7385 Actionable CI failure evidence

**Outcome:** a reviewer can repair a failing check without a human copying logs;
the collector remains repository/language-neutral as the issue amendment requires.

| Starting point and reuse | Change |
|---|---|
| [Reviewer checks][reviewer-checks] | Reuse exact-head and assignment rechecks, four-failure/4000-character bounds and existing response shape. |
| [GitHub observer][merge-observer] | JSON `get` explicitly refuses redirects; do not use it unchanged for a signed job-log download. |
| [Merge provider][merge-provider] | Reuse repository-scoped evidence token; its current [read-permission set][merge-read-permissions] omits Actions-read, so log access needs a scoped permission change as well as installation verification. |

**Implementation contract.** Preserve nonempty provider output. When blank,
fetch at most one bounded annotation page and job metadata for the exact
repository/check-run/head/workflow attempt. Include failure/warning annotations,
failed step name and last 60 lines of bounded job-log data, up to 4000 redacted
characters per failure, four failures total. Do not infer a job by check name
alone or use “latest run.” Use provider check/run/job identifiers and verify head
and attempt before inclusion. If job evidence cannot be correlated, return an
explicit unavailable reason plus safe annotations/link, not invented log detail.

Add a dedicated log downloader: bounded timeout/bytes (proposed 256 KiB tail
retention and 2 MiB maximum transfer), no ambient proxy configuration, bounded
redirect count, HTTPS only, approved GitHub artifact endpoints only. Authenticate
only the initial GitHub API request; **never forward the installation token to
the signed redirect URL**. Do not persist its query string. Stream/discard within
bounds and redact before serialization/logging. Strip terminal escapes and mask
authorization headers, signed-URL query values, known credential forms and
configured secret values; arbitrary text cannot be proven secret-free by regex,
so no raw log persistence and evidence stays within the authorized review scope.
Treat logs as untrusted data, never tool instructions.

403/404/expiry/download-size failure returns bounded `details` identifying
unavailability without making a red check green; a head/assignment change still
fails the request with existing typed conflict. No pytest parser, shard naming
assumption, unauthenticated user URL or broad token grant. Existing JSON shape
is retained, with optional `details_source`/`details_unavailable_reason` fields.

**Prerequisites/owner:** installation owner verifies Actions-read availability;
developer adds least-privilege evidence permission through the current scope
policy. Lack of permission blocks log enrichment/live proof, not unit work or
annotation fallback. Do not search for another credential.

**Deployment/recovery:** gateway only unless permission registration changes
are required; those follow the connection owner's process. Rollback leaves the
existing check output path. Preserve AC-01..03, SCN-01..03; include non-Python,
redirect-token, redaction, correlation and partial-unavailability tests.

### 7358 Reversible flow archiving

**Outcome:** owners can hide inactive work without deleting evidence, losing cost
or allowing work to continue unseen.

| Starting point and reuse | Change |
|---|---|
| [Flow model][models], [repository list][flow-repository] | Add nullable timezone-aware `archived_at`; keep internal slug lookup complete. |
| [Display state][display-state], [list route][flow-list-route] | Add archived display status and one shared filtered list/count predicate. |
| [Flow hook][flows-hook], [list page][flows-list], [CLI][flow-cli] | Add owner archive/unarchive controls, archive filter and documented CLI operations. |

**Implementation contract.** Proposed POST
`/api/orchestration/flows/{flow_id}/archive` and `/unarchive`, body
`{"reason":"No active work remains"}` with 8..4000 characters, PLAN_APPROVE and
tenant binding, return the existing flow summary with additive `archived_at` and
derived `status`. Archive/unarchive are idempotent 200 no-ops when already in the
requested state; only real transitions append `flow_archived`/`flow_unarchived`
with actor/reason. Read-only callers get 403; foreign tenant IDs stay 404.

Acquire the same flow serialization lock used by admission and recheck both
nodes **and durable executions/actions/claims**. Reject 409
`flow_has_active_work` for running, awaiting_merge or awaiting_gate nodes, and
for unresolved dispatch/review/deploy/evaluation effects or live authority even
when the graph node already says passed. The node-state-only check in the issue
would hide post-merge deployment. Archive is not cancellation. A concurrent
dispatch either reserves first and prevents archive, or sees archived and
refuses; no race may hide started work.

Default list excludes archives; `include_archived=true` includes them;
`status=archived` selects only archives (and implies inclusion). Pagination,
status chips, needs_me and attention/stall aggregates apply consistent predicates;
archives never contribute to attention/stall even when included. Detail, cost,
usage and decisions remain readable. Slugs remain reserved. Keep the internal
`list_flows` registration caller unfiltered so a name cannot be reused.
Gate/admission, dispatch, continuation runner and stall candidate queries refuse
new archive work; read-only receipt settlement/reconciliation may finish, so cost
does not become stranded. Unarchive does not change `execution_paused`, revive a
worker, reset attempt counters or renew an expired plan.

UI uses the existing graph/list controls and query invalidation; explains why an
archive is refused and that its slug stays reserved. CLI proposals:
`adp flow archive FLOW --reason TEXT`, `unarchive`, list `--include-archived`;
retain machine-readable output and nonzero exit on refusal. These commands are
**to be built**, not live run instructions.

**Compatibility/data:** additive migration and response fields (the issue's
“no shape change” claim is incorrect). Default listing intentionally changes.
Backfill is unnecessary: NULL means active. Single Alembic head and indexed
tenant/list predicate checked against the complete migration graph. Downgrade
only after unarchiving/reconciling archived rows or preserving their state in a
reviewed recovery procedure; merely reverting code could restart hidden work.

**Prerequisites/owner:** engine/UI/CLI developer owns all writers and aggregate
parity. All in-repository list callers were searched: route pagination and
registration slug lookup have different needs. External API consumers are not
enumerable here; release notes describe the new default.

**Deployment/recovery:** migration before gateway/tick readers/writers, then
frontend; packaged CLI update separately. Prefer roll-forward with archival
guards retained. Preserve AC-01..07, SCN-01..05; add post-merge-action and race cases.

### 7457 Deployment authority

**Ready only after platform-owner permission review.** A reviewed PR may select
how an already-authorized target is released; it must not authorize a new account,
role, environment or capability by adding a manifest entry. Keep production gates.

| Starting point and reuse | Change |
|---|---|
| [Manifest][deployment-manifest], [workflow snapshot][deployment-workflows] | Ordered path rules exist. Add versioned release-derived authority and complete mapping; retain unknown-path failure. |
| [Target resolver][deployment-target], [environment leases][leases] | Resolve through registered connection/provider identity and lease physical target before mutation; preserve cross-connection alias serialization. |
| [Workflow provider][workflow-provider], [runtime verification][runtime-reader] | Keep exact revision/blob/run correlation and observed runtime evidence, not workflow-success-only acceptance. |
| [Review cycle][review-cycle] | Add bounded developer repair fallback without resetting review/repair stage counts or accepting stale review. |

**Implementation contract.** Introduce proposed manifest schema v2 alongside
read-only-compatible v1 parsing. Manifest is loaded from the reviewed **merge
revision of the bound repository**, not from arbitrary head or only the packaged
gateway file. It describes stable component path patterns, deployment entry,
logical target/environment, workflow path, allowed input schema, verification
and optional rollback adapter. Runtime derived `artifact_revision` and
`definition_revision` both equal the verified merge SHA and are frozen in the
prepared deployment action. V1 already-prepared actions reconcile under their
recorded snapshot; do not reinterpret them or issue a second workflow.

Require verified PR/review/required checks and main ancestry at that merge,
current accepted `deploy` policy, fresh claim/lease, allowed target connection,
remaining allowance and all human gates. For GitHub's dispatch-ref limitation,
verify the selected executable workflow definition blob equals the reviewed
merge blob; use existing immutable source/definition inputs and guards. If the
provider cannot execute that definition/ref, fail typed rather than execute a
newer unreviewed definition. Freeze workflow ID/path, input digest, merge SHA,
plan/claim identity, connection, physical target, correlation and attempt in the
action; reconcile a lost acknowledgement by correlation before any redispatch.

**Connection decision:** reject automatic widening of legacy empty
`environment_connection_ids`. Provide a discovery projection of verified,
principal-visible connection labels/capabilities (no credentials) during plan
authoring. Reuse the [metadata-only credential list][credential-list] and
[verified-binding predicate][verified-connection], with proposed additive
`deployment_eligibility` metadata containing an approved environment binding,
verification revision and permitted actions. The gateway computes these facts
from registered policy/provider evidence; a credential label or client-submitted
metadata is not a verified environment binding. Plan-authoring UI/CLI consumes
this projection and displays “no eligible connection” rather than guessing.
The owner explicitly accepts concrete IDs or a new versioned
single-connection selection rule scoped to environment/provider/capabilities.
Resolve that rule once at acceptance, require exactly one authorized match,
freeze its ID into the accepted authority and reverify before use. Zero/multiple,
revoked, other-tenant, changed role/account or absent grant all refuse. An empty
legacy list remains “no deployment authority.” This is a deliberate safety
correction to the story's implicit single-connection fallback.

**Target readiness:** do not default `enabled` from connection existence or wait
until after first mutation to identify the target. For existing resources,
provider readback supplies canonical account/region/service/resource plus
cluster/namespace identity before lease acquisition. For first provisioning,
require explicitly accepted creation capability and a pre-mutation lease on the
canonical state/stack namespace; verified outputs can then bind resource IDs.
A reviewer-added workflow cannot mint this capability. Do not auto-enable
unresolved v1 entries. Disabled targets remain disabled until authorized setup.

**Coverage:** use [the exhaustive inventory](inventory.md), not broad markdown
or CI ignores. Runtime prompts/skills, Docker COPY dependencies, generated
contracts, shared pricing code and manifests affect releases. Permit one path
to affect multiple components where its consumers require it. Tests/non-runtime
records can be explicitly excluded, but a workflow filename ending `-ci.yml`
is not sufficient proof. Unmapped/ambiguous paths block with a list and proposed
manifest correction in the same reviewed PR. Mapping never itself grants scope.

**Input ownership:** source contains portable non-secret schemas/defaults;
protected environment configuration contains real target identifiers and secrets
remain in the existing vault/secret service. Define merge precedence as reviewed
portable defaults < selected protected target configuration; require typed input
validation and reject contradictory target identity. A reviewed code change can
add a defaultable input without a secret edit; a genuinely new target binding
is a real environment-owner decision. Never print merged configuration.

**Repair handoff:** if reviewer-owned repair still leaves CI red and repair
allowance remains, reserve one developer repair action bound to the existing PR,
head and structured failures. Fresh review/CI required after every head change.
Duplicate results do not dispatch another repair. Bootstrap exemptions come from
7559; this fallback does not reset attempts or bypass exhausted budget.

**Prerequisites/owner:** platform owner accepts the explicit connection-selection
and creation-capability model and v1/v2 rollout. Engine developer owns schema,
snapshot, leases, repair and negative tests. Manifest/target owners review audited
component coverage. No automatic consent migration for existing accepted plans.

**Deployment/recovery:** dual reader first, v2 manifests/proposals second,
activation last under accepted authority. Gateway/tick plus component-specific
workflows; no automatic infrastructure applies. Disable new v2 admission for
rollback but continue reconciling recorded v2 actions and held leases. Do not
roll back to code unable to read unresolved actions. Preserve AC-01..04; the
backlog supplies proposed SCN-01..04 because this story has no scenario table.

### 7513 Tools deployment

**Not ready under the existing role as written.** The role exists in source but
its broad allow-plus-deny design includes IAM role creation/policy/trust mutation,
KMS mutation and namespace-wide EKS edit. That contradicts the story's “no IAM
mutation” and named-resource Kubernetes boundary. Its gateway ARN local is not
a substitute for a resource-scoped allow. Do not call that contract satisfied.
The variable's default-off setting does not establish that it is inactive:
the [tracked environment overlay][automation-opt-in] already opts in. Actual
application of that overlay is unverified; the platform owner must reconcile
current release authority before enabling the redesigned release path.

| Starting point and reuse | Change |
|---|---|
| [Tools deployment role][tools-role], [context role][context-role], [gateway role][gateway-role] | Reuse protected-environment OIDC subject, default-off feature flag and tests, not their broad grants as justification. |
| [Trust inventory][trust-inventory] | Add missing `tools-interceptor-deploy.yml`; façade/platform already admitted. |
| [Tools infrastructure][tools-infra], [façade infrastructure][facade-infra] | Existing root creates IAM/KMS/ECR/DynamoDB/CodeBuild/AgentCore resources; release-only role cannot apply it unchanged. |
| [Interceptor release][interceptor-release] | Requires an existing configured function, publishes a numbered version and moves `live` with revision checks; no function definition exists under the tools platform root. |

**Implementation contract.** Split privileged bootstrap ownership from ordinary
release. Automation/platform state owner provisions IAM roles, key policies,
ECR/build foundation, exact Kubernetes Role/RoleBinding, interceptor execution
role/function/log group and target wiring using the approved infrastructure
process. Migrate existing Terraform ownership via reviewed moved/import/state
procedures, never two states managing the same resource. Routine tools release
may update existing deployable code, AgentCore configuration/targets and its
dedicated state only; it must not mutate IAM or KMS policy to get past a failure.
Platform plan must prove which resources remain in release state before enabling
its apply job. This is the necessary resolution of the issue's incompatible
“full platform Terraform apply” and “no IAM mutation” requirements.

Use explicit resource-scoped allows for release actions: façade repository and
build project/source prefix; selected cluster describe; named Deployment,
Service, ServiceAccount and existing scheduled workload if actually released;
interceptor function/version/alias; exact environment state object and lock;
registered gateway/targets; required input secret/parameter references. Split
non-resource-scopable APIs such as caller identity/ECR authorization into their
own minimal statements, with supported conditions and a documented exception.
Do not copy `AllowDeclaredAPIs Resource:*`. No role chaining. Dedicated Kubernetes
RBAC must prevent other workloads, roles, secrets, exec and token creation;
account for named-resource `create` restrictions by having bootstrap pre-create
objects or using reviewed admission controls. Namespace scoping alone is not
resource scoping. Test effective denies as well as statement presence.

Bootstrap the interceptor with a fail-closed placeholder **not attached as a
working security interceptor**. Release real Python package, verify numbered
version hash/configuration and alias, then configure the AgentCore gateway's
interceptor ARN and only then enable protected tool targets. The function needs
the established façade identity/assertion verification key configuration used by
the release script; derive these from approved bootstrap outputs, not issue text.
Façade smoke follows gateway/target wiring. A placeholder or green apply is not a
live tools acceptance result.

Protected GitHub environment and variables are created/verified by its owner
with branch restrictions and selected deployment identity. Do not publish their
values. `enable_tools_deployment` remains default false until scoped role/RBAC,
state split, source guards and tests pass. Existing portable configuration merge
is reused; replace target-specific checked-in inputs through the established
private configuration convention, not by copying real identifiers into this PR.

**Prerequisites/owner:** platform owner approves privilege split and state moves;
automation state owner applies reviewed infrastructure; repository environment
owner supplies protected bindings; tools owner validates façade/interceptor/
AgentCore integration. Live existence and effective permissions are unverified
here. Push-triggered direct delivery is independent of 7457 engine dispatch.

**Deployment/recovery:** manual approved bootstrap first, real interceptor
publication and verification, then platform/targets and façade rollout. Never
rerun historical deployment runs blindly against changing source/configuration:
select a reviewed revision and reconcile existing effects first. Roll back
interceptor alias to the prior verified numbered version; façade to prior digest;
gateway config via its reviewed state plan. Preserve state/evidence and avoid
destruction/key deletion. Preserve AC-01..05; proposed SCN-01..05 in backlog.

## Integration, decisions and completion

The [implementation backlog](backlog.md) preserves all current acceptance and
scenario IDs and states proposed additions separately. The [inventory](inventory.md)
contains complete tracked-artifact enumeration, deployment/build surfaces,
unresolved mapping gaps and reproducible checks. Inventory ownership labels are
not new platform catalog IDs; module/feature lineage remains unmapped in the
issues until the existing coordinator reconciles the catalog.

Recommended integration order:

1. 7559 and 7563 establish bounded startup/stall recovery; implement independently
   but serialize changes to shared action/authority/progress code.
2. 7571 and 7382 can proceed alongside them. Gateway settlement must precede
   worker rollout; receipt compatibility needs integration tests with preserved
   execution lineage and stage allowance readers.
3. 7381, 7385 and 7358 are independent feature fixes. Archive admission must
   integrate with every newly added retry/deploy/eval path before release.
4. 7457 authority decisions and 7513 infrastructure ownership can be reviewed in
   parallel. 7513 direct pipeline delivery does not depend on engine authority;
   engine-dispatched tools acceptance needs both, plus real verified targets.
5. Only after each required child live check: run epic AC-01 / SCN-01 for 24 hours
   with three actively progressing, privately selected flows, immutable tested
   revisions and a complete intervention ledger. Gate approvals are the only
   permitted operator actions. A dormant/all-blocked window cannot pass.

No source inspection, mocked test, workflow success or design review substitutes
for that live acceptance. The coordinator owns issue refinements and ordering;
the platform owner owns the permission decisions; implementation owners own
pre-review proof; the operator owns authorized rollout/live evidence. This design
run owns only the published, reviewable documentation PR.

## Source references

All source links below are pinned to the audit baseline. The generated inventories
also include Git blob identities, so a later branch movement cannot silently
change the evidence. External issue links are mutable historical context.

[stage-attempts]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/stage_attempts.py#L1
[review-dispatch]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/review_cycle_dispatch.py#L328
[review-cycle]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/review_cycle.py#L1
[startup-recovery]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/startup_recovery.py#L1
[bootstrap-failure]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/agentauth/bootstrap_failure.py#L1
[worker-bootstrap]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/agent-factory/agent-worker-image/entrypoint.py#L1214
[developer-recovery]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/developer_recovery.py#L58
[stall]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/stall.py#L566
[proxy-service]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/proxy/service.py#L479
[mantle-stream]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/proxy/mantle_service.py#L567
[proxy-routes]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/proxy/routes.py#L390
[lifespan]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/app.py#L251
[usage-service]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/usage/service.py#L117
[budget-receipt]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/shared/models/budget.py#L236
[reservations]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/budget/reservations.py#L78
[flow-meter]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/flow_meter.py#L93
[sigv4-proxy]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/agent-factory/agent/src/sigv4-proxy.ts#L140
[codex-config]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/agent-factory/agent-worker-image/codex-config.toml#L47
[plan-lineage]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/plan_lineage.py#L218
[shared-retry]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/shared_retry.py#L62
[amendment-route]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/routes.py#L741
[amendment-impact]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/amendment_impact.py#L1
[evaluation-plan]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/evaluation_plan.py#L52
[evaluation-controller]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/evaluation_controller.py#L268
[tick]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/tick.py#L358
[delivery-progress]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/delivery_progress.py#L186
[reviewer-checks]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/reviewer_checks.py#L116
[merge-observer]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/merge_evidence.py#L706
[merge-provider]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/merge_provider.py#L1
[merge-read-permissions]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/merge_evidence.py#L433
[models]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/models.py#L280
[authority-table]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/agent-factory/webhook-ingress/infra/dynamodb.tf#L296
[flow-repository]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/repository.py#L291
[display-state]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/display_state.py#L1
[flow-list-route]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/routes.py#L1155
[flows-hook]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/frontend/src/hooks/useFlows.ts#L1
[flows-list]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/frontend/src/pages/FlowsList.tsx#L1
[flow-cli]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/cli/adp-flow.py#L1
[deployment-manifest]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/deployment_manifest.py#L586
[deployment-workflows]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/deployment_workflows.py#L111
[deployment-target]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/deployment_target.py#L37
[leases]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/environment_leases.py#L1
[workflow-provider]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/deployment_workflow_provider.py#L1
[runtime-reader]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/orchestration/deployment_runtime_reader.py#L1
[tools-role]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/platform/automation-infra/tools-deployment.tf#L1
[context-role]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/platform/automation-infra/context-deployment.tf#L1
[gateway-role]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/platform/automation-infra/gateway-backend-deployment.tf#L1
[trust-inventory]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/platform/automation-infra/deployment-workflows.json#L54
[tools-infra]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/tools/platform/infra/main.tf#L1
[facade-infra]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/tools/platform/infra/facade.tf#L35
[interceptor-release]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/tools/interceptor/deploy.sh#L1
[automation-opt-in]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/environments/dev/automation-ci.tfvars.json#L48
[credential-list]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/auth/vault_routes.py#L253
[verified-connection]: https://github.com/aws-e/adp/blob/849f6163e59855c1554bc263e9ad96695730e9bd/modules/gateway/src/auth/aws_connection_authority.py#L60
