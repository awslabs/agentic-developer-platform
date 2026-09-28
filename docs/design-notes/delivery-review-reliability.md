# Delivery review continuation and failure recovery

Existing shared-worker flows must not be interpreted as protected executions
because a deployment flag changed. Review and merge handlers now resolve the
in-force accepted plan for each operation. A shared-worker plan selects the
shared receipt service only while shared continuation is enabled; protected
plans retain the protected service. Unknown contracts and changed plan versions
block. Disabling a transport reports the required operator action instead of
looking for another transport's execution records. Selection is not cached on
the handler, so one tick can inspect multiple flows without carrying authority
from one flow into another. Policy, claim, expiry, pause and provider checks
remain in the selected service.

Reviewer repairs validate the staged PR delta before final inspection. Git's
stdout and stderr diagnostics are retained. One correction may run in the same
repair thread and model-time allowance; the controller validates again and then
reviews the resulting tree. Persistent validation errors, changed Git identity,
and incomplete inspections never authorize publication or merge. The existing
post-inspection validation remains in place.

The reviewer emits a bounded structured failure on nonzero exit. Python uses
that cause for the run's failure summary and includes exit code or terminating
signal. Legacy stderr is supported. Engine-cycle failure logging distinguishes
an acknowledged queue message from the standalone queue retry path. This
failure document is diagnostic information, never a success/review receipt.

## Rollout boundaries

Deploy the gateway and tick together for plan-based selection, and deploy the
worker image for the reviewer and Python failure changes. Upgrade the gateway
terminal-report contract before deploying workers that send failure metadata. Keep stopped flows
paused during rollout. This patch changes no deployed configuration, accepted
plan, budget, policy expiry, run status or queue state.

In Embark 1 the observed plans use `shared_worker_role`, while shared continuation
is disabled. These flows will now report `shared_worker_continuation_disabled`.
Restoring execution still requires an operator-approved compatible transport or
an explicit authority migration. Do not create synthetic protected records or
turn off protected authority globally to clear the error. In particular,
initial developer admission and worker bootstrap have their own deployment
controls; selecting an existing shared review does not establish that new shared
worker admission is enabled. Verify these controls as part of the migration,
before releasing downstream stories.

Before a live canary, verify current PR heads, retained receipts, exited workers,
accepted policy expiry and remaining budget. Resume one selected flow under its
existing policy, observe a story through repair, CI, merge and persisted closure,
and verify that dependencies advance once. Configuration migration and this
live canary have not been performed by this source change.

Local regression coverage uses real PostgreSQL orchestration state and Git
repositories with local bare remotes. Model, queue and GitHub calls are fixtures;
it proves controller transitions and failure handling, not deployed model
availability or production completion.

## Additional reliability changes

Initial dispatch now reads the accepted continuation mode too. It refuses a
shared plan if its transport or reporting switch is disabled, even when protected
authority is enabled globally. Shared assignments retain their model selection
and SQL reporting capability through preparation, publication, and outbox replay;
they do not acquire a synthetic protected execution. Shared policy validation
still checks the human acceptance, configured worker role, time window and budget.
Worker bootstrap configuration must also support that accepted transport.

The existing GitHub renewal timer remains. Runtime callers can now force a
singleflight refresh when GitHub explicitly rejects a credential. API requests
and remote Git operations retry an explicit authentication rejection once with
fresh credentials. The original request body and expected-head constraints are
preserved. Network failures and uncertain write outcomes do not enter this retry:
merge delivery must still reconcile provider state. PATs remain externally
managed, and mediated workers cannot mint direct GitHub credentials.

CI and merge-queue polling now stop after one hour, returning the inspected head,
review and an explicit delivery blocker. Independently, the Python supervisor
limits either agent runtime to two hours, signals its process group, allows 30
seconds for shutdown and kills remaining process-group members. This is a runtime ceiling,
not an extension of a shorter policy or model budget. Existing checkpoint hooks
remain responsible for saving work; SIGKILL/OOM cannot guarantee a final save.

Developer failures write a bounded diagnostic before cleanup's explicit exit.
Known environment credentials and common GitHub/Bearer token forms are redacted.
The supervisor reads the record from a fresh per-run temporary directory and
retains the original cause in invocation diagnostics. Reviewer structured errors
also take precedence over token-manager informational logs. Terminal SQL receipts
can carry a validated category and exit code, without copying arbitrary provider
text or credentials. Failure spool replay retains these fields when no PR/review
evidence has superseded the execution marker. Existing candidate/review spools
retain their evidence rather than being overwritten by failure diagnostics.

Failure categories are diagnostic hints, never proof that another execution is
authorized. Automatic review recovery stops on recorded policy, provider-safety,
contract and cancellation failures. An operator can investigate and use the
existing explicit recovery path. Other recoveries still require positively
observed worker exit and current ownership, policy, budget and attempt fences.
Historical failures without the new metadata remain subject to the old gates.

## Disposition of the reported issues

| Reported issue | Decision and treatment |
| --- | --- |
| GitHub credentials expire/reject mid-run | Fix: retain proactive renewal and force bounded refresh on explicit authentication rejection, including remote Git. |
| Execution-policy expiry or exhausted budget | Preserve enforcement; refuse admission/recovery rather than silently renew authorization or spend. |
| First-call Claude failures | Historical cause remains unproven. Preserve original developer errors and durable categories; do not assume every first-call failure is model egress. |
| Provider cyber-safety refusal | Preserve the refusal and stop automatic retries. No bypass or automatic provider switch. |
| SSE interruption | Existing bounded same-thread resume and shared model deadline are retained and regression-tested. Invalid verdict JSON and refusals are not generalized into transport retries. |
| Very long runs | Fix: bound delivery polling and the entire worker process tree. Preserve available evidence and an explicit blocker. |
| Head movement / delayed PR projection | Existing bounded publication observation retries are retained; a genuinely changed head invalidates the prior review and requires reconciliation. |
| Git validation failure / ignored learning files | Fix validation before final inspection with one bounded correction. Keep conflict/whitespace checks and existing ignored-file staging rules. |
| Incomplete inspection / protected-path edits | Keep publication and merge blocked. These must not become warnings or approvals. |
| Invalid dispatch envelope | Retain current typed envelope validation; the historical `triggering_comment` failure was not reproduced in the current engine envelope builder. |
| Lost/killed worker | Retain positive-exit and checkpoint recovery fences; add process-tree deadlines and signal diagnostics. An OOM resource adjustment needs workload evidence during rollout. |
| Engine waiting indefinitely / stories not closing | Fix accepted-mode routing across initial dispatch, review/merge and shared outbox handling. Completion still requires actual provider merge and verified engine closure. |
| Missing/null failure evidence | Fix original-cause capture and durable failure metadata. This does not reconstruct missing historical transcripts or guarantee full logs for an abruptly lost pod. |

These changes do not prove every historical error has one cause. The live test
must verify deployed transport compatibility, actual model access, token renewal,
CI/merge and final story/dependency transitions before broader flow resumption.
