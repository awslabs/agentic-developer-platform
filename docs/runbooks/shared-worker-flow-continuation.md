# Continue an existing flow with the shared worker role

Once resumed, existing policyless flows keep their behavior until a plan approver
accepts a continuation. The continuation uses the existing execution runner and its action
ledger for review, repair, another review of the new commit, and optionally merge.
Developer and reviewer run IDs remain distinct. They may use the same GitHub App.

Deploy the gateway, tick, report-capable worker, and run-report migration together.
Configure `AGENT_WORKER_ROLE_ARN` with the actual worker IAM role and deliver the
same `AGENT_RUN_CREDENTIAL_KEY` to gateway and tick through the existing secret
mechanism. Enable `ADP_SHARED_RUN_REPORTING_ENABLED` and
`ADP_SHARED_WORKER_CONTINUATION_ENABLED` only after that cohort is verified.
The execution runner, dispatch, budget reservations, and normal provider checks
must be available. These switches do not authorize a policyless flow to merge.

Both operator routes require `PLAN_APPROVE` in the authenticated tenant:

- `POST /orchestration/flows/{flow_id}/continuation/preview`
- `POST /orchestration/flows/{flow_id}/continuation/accept`

The request contains an unstamped v2 `execution_policy`, the exact
`worker_role_arn`, `reconciled_spend_usd`, `reconciliation_evidence`, and
`effects_and_credentials_reconciled: true`. The policy must explicitly accept the
worker role through `user_credentials`, using `permission_mode: user_configured`
and `lifetime: provider_managed`. Allowed actions are limited to develop, review,
repair, and merge. Review and repair are required; merge must be explicitly
allowed and remains subject to any declared human gate and GitHub repository
rules. No deployment or machine evaluation permission is added by this operation.

The preview lists the next stage of each story, retained gates, current PR head,
and all migration blockers. It makes no changes. A running or unverified old
worker prevents acceptance; wait for its observed exit and preview again. Missing
bindings must be registered or recovered first. Previously passed nodes, failed
or halted nodes, and human gates retain their state.

Accept the same request with `expected_snapshot` set to the preview's `snapshot`.
A changed node, dependency, binding, provider head, run status, or requested
authority returns `snapshot_changed`. Repeat preview before accepting a different
snapshot. Lost-response retries of an accepted request return the same decision
and execution identities. Acceptance writes a new immutable accepted-plan version
without rebuilding the graph or resetting story attempts.

For a legacy flow with no accepted policy, continuation starts a new, explicitly bounded wall-clock window.
Historical attempt counts remain consumed and reconciled historical spend seeds
the existing flow meter. Unknown spend is not inferred as zero: the approver must
provide the reconciliation amount and its evidence. Failed meter initialization
prevents acceptance, and a missing meter after acceptance blocks further work.

Before a worker starts, policy and ownership refusals are recorded as attributed
`transition_rejected` decisions after the unused claim reservation rolls back.
The graph's `delivery_progress` shows stage `admission`, the typed blocker, the
responsible actor, the next required action and the observation time. For example,
`budget_unavailable` asks the platform operator to restore the existing meter;
it does not grant a new allowance. The story remains ready at its current attempt,
with no new claim, execution or dispatch. These diagnostics apply only to the
same attempt and accepted plan/policy, and a later dispatch supersedes them.
They do not claim that a retry is scheduled or expose provider error payloads.

The budget scope is `authenticated_gateway_calls`. The shared IAM role retains
its configured AWS permissions, so this does not promise a cap on direct provider
calls outside the gateway. Reporting capabilities authenticate one assignment;
they do not reduce the IAM role's permissions. They are never returned in graph
or decision responses.

After acceptance, inspect `/orchestration/flows/{flow_id}/execution`. Existing PRs
start at `awaiting_review`; fresh pending stories retain normal dependency
admission. Repair reuses the PR and its story attempt, consumes another durable
action, and requires a fresh reviewer run on the new commit. Successful merge
still requires current structured review evidence, passing required checks, and
GitHub's configured merge rules. Deployment and live acceptance remain separate
gates.

An already accepted flow that has **never started** can use the same preview and
accept endpoints with `preserve_accepted_policy: true`. Copy its in-force
`execution_policy`, remove only the server-stamped `policy_id`, `policy_hash`, and
`principal_id`, set `schema_version: 2`, and add the explicitly selected worker
role in `user_credentials`. Retain every original limit, fixed expiry, repository
and team scope, action, human gate, and `evaluation_acceptance` entry. Include the
normal reconciliation evidence and amount; missing spend is never inferred as
zero. The preview rejects any other policy change.

This operation verifies the original human acceptance and policy hash. Any prior
nonzero attempt, worker report, dispatch, PR association, work claim, or execution
record prevents this narrow adoption, including completed or expired history.
An initial gate approval through the CLI or UI is also supported: the adapter
verifies the bound draft hash, structural acceptance gate, human decision and
exact resulting policy. Superseded draft addresses with zero attempts can remain
as history when absent from the current plan; any worker or delivery history
still prevents adoption.
The new version links the original policy and decision, preserves the original
acceptance time for wall-clock enforcement, and does not reset the expiry,
attempts, or spend allowance. Its new human acceptance only selects the shared
worker transport. Existing evaluation mappings are retained; they do not add
`evaluate` authority or satisfy an evaluation. Human gates and evaluation nodes
retain their state and completion requirements.

An approver may additionally select `delivery_mode: "code_only"` in the request.
The preview and accepted continuation marker record this choice: a story can
finish after verified merge evidence, current structured review, and required
checks, while separate evaluation nodes retain their original evidence
requirements. Omitting the field preserves the existing delivery lifecycle.
Changing this choice requires a new preview; it never grants deployment or
evaluation authority.

For an inert registered draft, use `accept_draft_policy: true` with
`delivery_mode: "code_only"` instead of `preserve_accepted_policy`. This explicitly
accepts its initial gate and shared transport together. Keep the proposed limits,
fixed expiry, scope, human gates for code actions, and evaluation map. The request
must contain exactly the proposed code actions; `evaluate`, if proposed, is
deferred until a real evaluation contract is accepted. No worker or delivery
history may exist, and the live graph must still match the reviewed draft.

The preview identifies the single structural initial acceptance gate. Acceptance
records its human approval in the same transaction as the policy, shared meter,
and new plan version. Other gates and evaluation nodes retain their state. The
proposed policy remains in the historical draft; it is removed from the new
version so a later gate answer cannot overwrite the accepted shared transport.
Ordinary continuation refuses a draft that still carries inert proposed bounds.

## Pause or resume one flow

Use **Pause flow** / **Resume flow** in the flow list (`/flows`) or flow detail.
The **Execution: Paused / Enabled** indicator is separate from story progress.
The operator needs `PLAN_APPROVE` and an active human session in the flow's tenant.
The equivalent API is `POST /orchestration/flows/{flow_id}/execution` with
`{"paused": true}` or `{"paused": false}`. GET on that path remains the execution
ledger. Flow list and graph responses also carry `execution_paused`.

Pause stops admission of new developer, reviewer, repair and retry work for that
flow, including dispatch outbox replay. The existing flow-row lock orders pause
against dispatch admission: if pause commits first, no attempt is reserved; if a
dispatch was already admitted, that dispatch can finish queuing. Queues and workers
remain active. Already queued or running work can finish, publish evidence and
merge its PR. The engine continues observing results and completing merged stories
while paused. Waiting outbox assignments do not become stalled merely because the
flow is paused.

Resume changes only this flag. It preserves story states, PRs, claims, budgets,
attempt counters and the runner's three-attempt ceiling. It does not clear a stall,
approve a gate, renew expired credentials or extend policy/execution deadlines.
Existing blocked work still needs its normal recovery. Human plan authoring and
approval controls remain available while execution is paused.

The global engine switch and scheduler remain the master controls. **Enabled** on
a flow means it is eligible when the global engine runs; it does not switch the
engine on. Resuming one flow never resumes other flows or automatic GitHub PR reviews.
Direct issue mentions and labels remain available independently of engine/flow
pause. Keep the shared `POST /github` invocation permission in place; disable
automatic PR reviews using `GITHUB_AUTO_PR_REVIEW_ENABLED=false`.
Repeated identical requests are harmless; changed values append a human audit
record (`flow_paused` / `flow_resumed`).

Migration `067_flow_execution_pause` starts **all existing and new flows paused**.
For rollout, keep the global scheduler/tick and GitHub-triggered review path off,
apply the migration, then deploy the updated gateway, engine/tick and frontend.
An old engine ignores the new column, so replace it before restarting the scheduler.
Check that all flows show Paused, resume only the selected test flows, then enable
the global engine through the normal deployment procedure. Keep queues active.
No runtime switch is changed by installing the migration or by these UI controls.

## Increase retries on an active shared flow

An authenticated human platform admin with `PLAN_APPROVE` can raise the attempt
ceiling without replacing the accepted plan:

- `POST /orchestration/flows/{flow_id}/retry/preview`
- `POST /orchestration/flows/{flow_id}/retry/accept`

Send `expected_plan_version`, `expected_plan_hash`, `max_attempts_per_node`
(an integer from 1 to 100, greater than the current limit), and an attributed
`reason`. Accept the same request with the preview's `snapshot` in
`expected_snapshot`. Acceptance appends a `retry_limit_increased` decision tied
to the exact accepted plan and original policy hash. Concurrent approvals or a
changed plan invalidate the preview. A later plan acceptance does not inherit it.

The value is the policy's total attempt ceiling, including attempts already
consumed. Story dispatch and review/repair admission use it. The execution runner
also enforces `ORCH_RUNNER_MAX_ATTEMPTS` (default **3**) on continuation attempts;
a retry supplement cannot raise that limit. Once exhausted, the story displays
as **stalled** and the runner cannot dispatch another agent. Waiting and recording
an already-completed merge do not consume another attempt. Notification delivery
retries retain their configured limit.
Existing story and execution counters, assignments, claims, expiry, concurrency,
spending limits, and gates are retained. Failed stories still require normal
operator resume; active blocked executions are rechecked by the engine.

This approval neither reads nor changes the usage meter. It can be recorded while
usage is unknown, but budget admission continues to block work until accounting
is recovered. Financial supplements retain their run and chain limits. In-flight
model uploads tolerate only verified increases to retry or financial limits;
other policy changes still refuse the request.

## Switch budget enforcement off

Platform administrators can now switch financial enforcement off without changing
an accepted plan or increasing its dollar limits:

- **Across ADP:** Budget & Spend → Manage budgets → Global budget enforcement.
- **One flow:** open the flow page → Flow budget enforcement.

Global off takes precedence over every flow setting. With global enforcement on,
a flow can independently opt out. The UI shows both the selected flow setting and
the effective state. Changes apply at the next model request or engine admission;
they do not restart failed stories, extend expiry, or reset attempts.

Usage and cost reporting continue when enforcement is off. Request observations
use the existing accounting keys and actual-usage settlement, without budget
quotes, cap checks, or waits for earlier unsettled requests. Existing usage,
reservations and unresolved charges are retained. Unbounded in-flight observations
remain unknown until a trusted usage receipt arrives. Turning enforcement back on
uses that history and can block again if usage remains unknown or exceeds a cap.
A failed observation is recorded in `budget_accounting_gaps`; changing a switch
never clears it. Reconciliation must establish the missing usage before clearing
such a record; do not delete gaps or reset meters to manufacture headroom.

Authentication, current assignment, membership, repository scope, ownership,
attempt limits, concurrency, expiry, review, and merge gates remain in force.
Budget off does not grant permission to perform an action or mark a story passed.

The human API provides `GET` and `POST` at `/budget/enforcement` and
`/budget/enforcement/flows/{flow_id}`. POST accepts `enabled`,
`expected_revision` (from GET), and `reason`. Concurrent stale writes return 409.
Changes require a live human platform-admin session and budget-update authority;
flow changes also require plan-approval authority. Each change is audited.
Flow selection on the model path comes from the authenticated server assignment,
never a flow header. The flow API only addresses flows in the current workspace.

Absent an explicit global setting, `BUDGET_ENFORCEMENT_ENABLED` remains the
installation default (true when absent). A saved global setting overrides that
default. Unlike the old flag behavior, off keeps model identity and cost tracking
active and does not itself produce `budget_unavailable` for governed flows.
## Recovering a timed-out review

The scheduled tick retries an unstarted review or repair using its immutable
assignment, run ID, PR head and start-once reporting capability. It validates the
current accepted plan and claim before sending. Started or terminal assignments
are not restarted by this outbox. Reviews get capacity before new development.
The stall clock follows the currently assigned worker; a completed developer's
old pod deadline does not fail its review or merge phase.

For an existing story marked failed by `node_stalled`, a human plan approver can
restore its **same** continuation through
`POST /orchestration/nodes/{node_id}/resume-continuation` with:

```json
{
  "expected_attempt": 1,
  "expected_plan_version": 3,
  "expected_run_id": "the-current-assigned-run",
  "reason": "Startup problem repaired; resume the existing review assignment."
}
```

Read the current run reports, execution and bound PR first. This operation keeps
the attempt, claim generation, PR, receipts, usage and accepted policy unchanged.
It refuses worker failures, halted nodes, expired authority and superseded runs.
It neither reports completion nor approves a review. Verify an acknowledged
worker start and then actual review evidence after recovery. A worker that exited
without a terminal receipt still requires evidenced ownership/effect recovery;
absence of activity is not permission to replace it.

## Correct dependencies in future waves

While the flow is paused, its policy owner can preview and accept explicit edge
changes through `/orchestration/flows/{flow_id}/wave-dependencies/preview` and
`/accept`. Only prerequisites of waves with no execution or accepted evaluation
history may change. Every started wave is frozen in full; active workers may
continue and finish. Acceptance preserves their original assignments, the
existing evaluation waiver, effective policy supplements and budget posture.

See [the dependency amendment contract](../design-notes/paused-wave-dependency-amendments.md)
for request examples, preview tokens, lock conflicts and scope restrictions.
The flow remains paused after acceptance; review the accepted graph before
resuming admission.

### Gateway / scheduled-engine release parity

The gateway Deployment and `adp-<environment>-orchestration-tick` Lambda execute
code from the same image, but require separate deployments. Building or pushing
an ECR tag does not update Lambda. A cancelled workflow or a manual EKS rollout
can leave the two consumers on different revisions, including incompatible plan
lineage handling. A flow pause does not suspend observation or stall detection.

Both `gateway-deploy.yml` and `platform/scripts/deploy-all.sh` now run the shared
alignment helper and verify parity before declaring the gateway release complete.
For a manual gateway rollout, run this immediately after migrations and EKS
rollout, using the intended release image and the target cluster's kubeconfig:

```bash
python3 modules/gateway/scripts/sync-gateway-engine.py \
  --image "$GATEWAY_IMAGE" --account "$ACCOUNT_ID" --region "$AWS_REGION" \
  --environment "$ENVIRONMENT" --namespace adp-gateway
```

Repeat with `--verify-only` before recording release success, and after recovering
an interrupted deployment. The helper resolves the release to an immutable digest,
checks the active AWS account and gateway rollout, updates Lambda with a revision
fence, waits for AWS completion, and rechecks both consumers. A missing function,
permission error, unsuccessful update, or digest mismatch fails the release. This
path expects the scheduled engine to be provisioned; an intentionally engine-free
installation requires a separately scoped deployment, not a silent missing-engine
success. The helper does not invoke the engine, change schedules, resume flows,
or repair previously failed stories.

These are completion checks, not an atomic cross-service deployment: cancellation
or a concurrent manual update can still interrupt the rollout. Such a release is
incomplete until the helper succeeds. Never substitute a successful ECR build or
EKS health check for this verification.

### Automatic stalled-story review

For accepted shared-worker flows, the tick can restore a story whose latest
state transition is `node_stalled` and assign `agent-codex-reviewer` through the
existing review-cycle runner. The flow must be unpaused, the policy must permit
review, its expiry/budget must remain valid, and the initial attempt plus recorded
continuation attempts must be below `max_attempts_per_node`. Recovery does not
increase this ceiling or clear a halt. An explicit reviewer result/blocker or an
unreconciled pending effect remains held for reconciliation.

The prior worker must have a positively exited status in the exact, consistently
read run-registry record. An old heartbeat, missing pod, elapsed timeout, or
unavailable registry is insufficient. The old receipts are retained unchanged;
the existing claim-transfer transaction fences them when the reviewer starts.

If a bound PR exists, recovery retains it. If there is no PR, the engine resolves
the original dispatch's repository/installation/story, verifies the corresponding
`agent/issue-<number>` branch head, and records `recovery_pr_prepared` before any
GitHub mutation. With both review and repair permission, it creates a draft PR
(or adopts the single matching open PR after a lost response), then registers the
real provider identity. A missing/moved checkpoint or ambiguous PR leaves the
story blocked. This step creates no model run and fabricates no completion.

The reviewer reads the current story/clarifications, checks the preserved work,
repairs missing acceptance behavior within its authority, and uses the existing
review/check/merge path. Only a complete recorded review and current checks may
promote an engine recovery draft. Existing PR drafts retain their normal policy.
A new reviewer consumes the same continuation counter as other reviews/repairs;
there is no separate recovery budget or retry transport.

`review_recovery_requested` records service identity and the verified exit/scope;
`stalled_review_blocked` records a bounded failure code without provider secrets.
The pass samples at most 100 unpaused failed stories and allows 30 seconds for
candidate recovery work per tick, so repeated blockers cannot monopolize selection.
A code deployment does not itself resume a paused flow or declare old work passed.

Roll out the matching gateway/tick and worker images together. The tick also needs
the Terraform `EngineRecoveryReads` grant: GetItem on initial `orch:*` and UUID
continuation records in the configured webhook-events table, with no Scan or
Query grant. Apply this IAM change before relying on autonomous recovery. Gateway
and Lambda image parity must be verified using the deployment helper above.
