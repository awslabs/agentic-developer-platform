# Execute delivery flows through the UI

EPIC A (#4910) is the acceptance case: its current graph has 27 stories, 13 gates
and four evaluations. On main `ec10ead9`, ten tick/dispatch cycles leave its root
gate at `ready` and all stories pending. Gate arming is one missing boundary;
evaluation dispatch and recording completed work are also necessary for a usable loop.

## Decisions

A ready human gate advances through the existing `ready -> running -> awaiting_gate`
edges in one transaction. No worker is created, and no attempt is charged. The engine
only presents the question; existing dashboard/GitHub approval adapters remain the
only way to answer. Pending dependencies, rejection, halt and supersession continue
to block progress. This also recovers ready gates in already registered drafts.

Evaluations execute through the existing agent queue as the operations persona, using
an explicitly linked evaluation issue. An evaluation without an issue is an actionable
configuration error, never a successful no-op. The four existing evaluation drafts for
Superplane become the evaluation issues; a second set of wave orchestrators must not
run over the same stories. The accepted proposal pins the issue references.

Engine dispatch must produce a complete worker envelope and persist the run's identity
before publishing it: stable per-attempt message id, tenant, approver, correlation,
source and graph address. A row saying the worker ended does not establish that its
work passed. Completion must carry reviewed source or evaluation evidence and be bound
to the current node attempt; stale results, skips and missing evidence never pass.

A completed story whose merge/checks are not yet verified enters `awaiting_merge`.
This is still in progress, blocks every dependent node, and is not a worker stall:
waiting for review must not cause a second agent attempt. Only verified evidence
advances it to passed. No new edge allows services to answer human gates.

The result observer polls a bounded batch, oldest observation first, with a
persisted audit cursor so active runs cannot starve later attempts.

The UI must expose answerable checkpoints, execution/evidence state and retry controls.
The existing immutable plan-amendment API is used to replace the stale Superplane draft;
changes must retain a human acceptance checkpoint and not dispatch the old graph.

## Verification and delivery

Exercise gate ordering, tenant isolation, concurrent ticks/answers, failure/retry,
evaluation dispatch/results and an entire two-wave loop through the UI API. Also run
the full Superplane topology through the real tick with isolated database fixtures.
Runtime validation must distinguish simulated task results from actual agent/test runs.

The scheduled tick is a Lambda using the gateway image. Publishing a new EKS gateway
alone does not update that Lambda; delivery must verify both runtime image revisions.
No Superplane cloud provisioning, state migration or retirement is authorized by this
engine repair. Those remain explicit gates on the Superplane plan.

## Verified locally

The orchestration suite passes 1,237 tests, including a two-wave flow through
real HTTP approval/rejection/resume routes and the complete Superplane r2 graph
with simulated external execution (27 story dispatches, four operations
dispatches, no gate workers). A separate regression verifies that missing
configuration does not consume execution slots. The frontend has 119 passing
control/graph/feature-loading tests and a successful production build. Python lint/format,
Terraform format and deployment workflow YAML checks pass.

Live rollout must first apply the additive engine-run IAM permissions to the
existing tick role, then publish the same gateway image to EKS and the tick
Lambda. Verify its resolved digest and `results_examined`/`result_errors` tick
metrics. No schema migration is needed: node states and decision kinds are
stored as strings. Existing pre-fix running attempts have no dispatch/run
binding; they require the existing human retry control after stall detection.

The live browser check also reproduced a route-loading race: `/flows/:id` was
redirected to Dashboard while the fail-closed feature flag was still loading,
even when the server enabled orchestration. FeatureGate now waits for that
request without exposing controls or discarding the URL; a confirmed disabled
flag still redirects. Deferred-response tests cover both outcomes.

The scoped engine IAM apply succeeded in account 879318057152 via workflow
34784999424. It changed only the targeted tick policy; this is not a claim that
unrelated infrastructure has converged.
