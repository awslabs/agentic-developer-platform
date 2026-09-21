# Continue an existing flow with the shared worker role

Existing policyless flows keep their behavior until a plan approver accepts a
continuation. The continuation uses the existing execution runner and its action
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
