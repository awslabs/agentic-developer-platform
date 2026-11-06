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

A clean target-branch advance during CI is revalidated by the retained reviewer
against the same published PR head. It does not manufacture another source
repair or consume a repair retry. Pending CI still waits, and passing CI only
supplies evidence to the reviewer; it never erases findings automatically.
Conflicts and provider-required strict-base integration still enter the repair
path. Merge delivery continues to require the exact reviewed head and current
base, and rechecks the target immediately before the provider mutation.

## Owner recovery for protected reviewers

The existing node `review-recovery/preview` and `review-recovery/accept` endpoints
also support protected-worker plans. The authenticated policy owner may request
another review after the current protected reviewer records a terminal failure.
The preview binds the current plan, claim, attempt, PR head and terminal receipt;
acceptance records an append-only `protected_review_recovery` decision. It does
not mark delivery successful, change a failed worker receipt, reset attempts or
increase spending authority. The normal dispatcher rechecks authorization,
remaining allowance, ownership, head and terminal evidence before starting a
new reviewer. A later failure needs a separate owner recovery decision.

## Review retained work after failed development

For a protected-worker story whose developer attempts have failed but left an
implementation PR, the policy owner may explicitly select
`mode: "review_existing_work"`. First recover the implementation's PR binding
through the attributed human binding control. Verify the provider repository and
PR IDs, current head, accepted story scope and original producing run. A PR body
without a correlation marker does not acquire one through recovery: the human
association and durable original node-dispatch evidence supply the provenance.

Use the existing `POST /orchestration/nodes/{node_id}/review-recovery/preview`
endpoint with these fields, then review its returned snapshot and expiry:

| Field | Value |
| --- | --- |
| `mode` | `review_existing_work` |
| `source_run_id` | Original failed developer run that produced the retained work |
| `expected_run_id` | Current, final failed developer assignment |
| `expected_attempt` | Current development attempt, without resetting it |
| `expected_plan_version` | Current accepted policy plan version |
| `expected_head_sha` | Exact current 40-character PR head |
| `reason` | Owner's reason for associating and reviewing this retained work |

Send the same fields plus the returned `expected_snapshot` value (from
`snapshot`) to `POST /orchestration/nodes/{node_id}/review-recovery/accept`.
Authentication must be a human session with plan-approval permission and current
policy ownership. The server records `retained_work_review_recovery`, reopens the
same execution and reserves a newer generation of the same claim. Three failed
developer attempts stay three; all failed protected receipts stay unchanged.
The recovery authorizes one real reviewer, with no automatic story-repair grant.
Normal review evidence, CI, merge and closure checks still apply.

The snapshot expires at the next server 15-minute boundary, or earlier when its
policy or grant expires. Acceptance and dispatch recheck current source lineage,
policy, head, ownership, review-stage allowance and spending authority. The
original flow chain meter must be readable and have at least the current run cap
remaining, as must the flow meter. Legacy chain hashes may be read without an
initialization anchor only when they contain valid charge entries and no unknown
accounting. Missing meters, orphan markers, unbounded usage and malformed values
refuse recovery. No cap, reservation or chain identity is reset by this control.

If an accepted authorization expires before dispatch preparation commits, submit
a fresh preview with the same fields and `renew_decision_id` set to the original
`retained_work_review_recovery` decision ID. Review and accept its new snapshot.
This creates a separate `retained_work_review_renewal`; the original receipt,
claim generation, attempt totals and any uncommitted operation key remain intact.
Renewal requires the same owner, still-current original facts, and an expired
window. It refuses once a dispatch receipt or protected successor exists, or
another run owns the claim. Publication uncertainty must be reconciled through
the existing dispatch path and cannot authorize a replacement reviewer.

Renewal extends the time window only. It cannot adopt a changed PR head, policy
or cap after acceptance. Such a change preserves the existing intent and needs a
separate audited recovery capability, which this control does not implement.
Before acceptance, inspect changed facts or obtain the required budget approval
and then request a fresh preview. A cap denial during dispatch persists an
explicit budget block, with the existing action and spent attempts retained.

| Refusal | Operator action |
| --- | --- |
| `operator_bound_delivery_required` | Verify and recover the exact implementation PR association as a human. |
| `retained_source_*`, `report_assignment_changed`, `recovery_authority_changed` | Recheck original/current run lineage and accepted authority; do not rewrite historical receipts. |
| `current_pr_changed`, `recovery_preview_changed` | Before acceptance, inspect current facts and obtain a new preview. After acceptance, reconcile changed facts against the pinned recovery. |
| `retained_budget_exhausted` | Review spend and reservations; obtain any required financial approval. Changed limits after acceptance require reconciliation, not renewal. |
| `retained_budget_unavailable` | Reconcile accounting from trusted evidence; never initialize a missing historical meter as zero. |
| `retained_recovery_expired_renew_unstarted` | Request explicit renewal; the server will first prove dispatch remains unstarted. |
| `retained_renewal_not_expired` | Use the current authorization while it remains valid. |
| `retained_recovery_expired_reconciliation_required`, `retained_renewal_requires_reconciliation` | Reconcile the existing receipt, protected successor and queue outcome; renewal is unavailable. |
| `continuation_attempts_exhausted` | Review the independently consumed review-stage allowance; this control never resets it. |

## Restore a prematurely released retained reviewer

If a retained-work reviewer exited failed and its claim was released while the
same execution remained awaiting review, use the existing review-recovery
preview and accept endpoints with `mode: "review_released_reviewer"`. Set
`expected_run_id` to that failed reviewer, and supply the current
`expected_attempt`, `expected_plan_version`, `expected_head_sha` and owner
`reason`. Omit `source_run_id` and `reviewer_model`: the server derives the
original source and preserves the retained review-only authority.

Preview reads the existing meters without reconciling or initializing them. It
pins the exact protected terminal workload and grant, original dispatch action
and receipt, retained-work recovery and source, review findings, current claim,
PR binding and policy. Unknown effects, a newer dispatch or retired dispatch
ambiguity require reconciliation. Acceptance requires the preview's
`expected_snapshot` and the current policy owner. It reserves the same claim at
exactly the next generation and wakes the same execution. Developer attempts,
execution attempts, original receipts, spend, author and correlation chain stay
unchanged. The successor is one sibling reviewer under the verified original
parent, with the prior findings and no additional repair authority (#8142,
#8063).

The same finite 15-minute window applies. An expired reservation can be renewed
through a new preview and accept with `renew_decision_id` set to the original
`released_reviewer_recovery` decision. The server proves that this recovery's
successor remains unstarted; prior succeeded reviews remain immutable history.
Renewal preserves the claim generation and any uncommitted operation key.
`released_reviewer_expired_reconciliation_required` or
`released_reviewer_renewal_requires_reconciliation` means a dispatch receipt,
protected successor or changed owner may already exist: reconcile the existing
operation instead of accepting another recovery. Replaying an acceptance never
extends its expiry or creates another successor.

New failed-reviewer callbacks retain an unfinished, exactly committed reviewer
claim even when the original developer handoff is absent. Failed developers
continue to use the existing failure and retained-work recovery path.
