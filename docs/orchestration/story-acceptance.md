# Story acceptance and automatic retry recovery

A verified merge completes code delivery. Deployment and dependent evaluation
may still be outstanding. Story cards show an **Engine acceptance** checklist
after the merge controller settles a verified receipt. Passed checks remain
visible, and the earlier development/review journey is expandable.

The checklist records merge identity, review evidence and applicable CI
requirements. Code-only stories then conclude. Deployment stories additionally
show combined deployment/runtime verification and final delivery completion.
The deployment controller verifies these together; the UI does not invent
separate successful checks. A documentation-only receipt explicitly says that
no deployment was required. Waiting for an evaluation assigned to a separate
node is identified in the completion check.

## Evidence and reads

`story_acceptance.py` stores a display projection under the fixed
`story-acceptance:v1` action key in the existing execution ledger. Merge,
deployment and successful completion controllers write it in their fenced
settlement transactions. Original receipts remain the audit evidence and
acceptance authority. This projection never authorizes an action.

The projection is bound to the execution, attempt, accepted plan, PR binding,
source head and accepted scope digest. The graph reads all current projections
in one query, independently of the execution panel's latest 20 actions. Existing
graph polling updates the checklist; checks make no provider calls or individual
requests. Verified facts survive later observations. Different attempts,
unpreserved plans and changed bindings cannot inherit those facts.

Old accepted stories without this record explicitly say detailed checks were
not recorded. They are not given fabricated passing checks. An older delivery
still in progress retains its current diagnosis without claiming acceptance.

## Recovery control

The flow detail page offers **Automatic retry recovery**. Its authenticated,
tenant-scoped endpoint is `POST /orchestration/flows/{flow_id}/recovery` with an
`enabled` boolean. Changes are audited. The switch adds a veto to the existing
developer/operations automatic retry path; enabling it grants no new actions,
budgets, attempts or targets. The default preserves policy-authorized existing
retry behavior without changing accepted plan serialization or hashes.

Disabling recovery also holds an automatic retry that was scheduled but has not
yet been dispatched. Observations and already-dispatched work can finish. A
paused flow stays paused. Explicit human recovery routes retain their separate
authorization and are not invoked using an engine impersonation of a user.

For operations turn/context limits, the engine validates any saved checkpoint
against the authenticated assignment and retained plan lineage before retrying.
The retry records its checkpoint revision and runtime diagnostic. The existing
worker restore path continues the task board, preserving acknowledged task IDs.
An invalid checkpoint produces a platform reconciliation reason. All normal
settled-worker, policy, attempt, deadline and dispatch-budget checks still apply.
The UI correctly identifies the per-invocation turn limit as a platform setting.

This change does not add a Task API diagnostic agent, automatic protected-input
provisioning or financial reapproval. Those require their own authority and
evidence contracts. Existing deterministic merge/workflow recovery is reused.

## Verification

Regression coverage includes code-only and deployment stories, retained plan
lineage, completion after dependent evaluation, more than 20 intervening
actions, tenant isolation, historical missing detail, checkpoint integrity,
attempt and pause boundaries, and disabling a retry before dispatch. PostgreSQL
controller tests exercise real settlement transactions. Frontend tests cover
visible passed checks, incremental updates, recovery controls and expandable
history; browser checks cover narrow and desktop layouts.
