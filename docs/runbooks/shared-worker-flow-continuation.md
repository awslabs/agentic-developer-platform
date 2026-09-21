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

The accepted continuation starts a new, explicitly bounded wall-clock window.
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
