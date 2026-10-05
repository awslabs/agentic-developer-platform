# Owner-approved evaluation exceptions

An owner may explicitly waive one unconfigured, unexecuted machine evaluation
on an accepted shared-worker flow. This accepts the missing independent
verification; it does not establish a test result, review, deployment or live
acceptance. The evaluation retains its identity and becomes `waived`, distinct
from `passed`. The graph labels it **Waived by owner approval** with a neutral
symbol, reason and approval time. Tracker projections also identify the waiver.

Use the existing authenticated human session with `PLAN_APPROVE`. The approver
must be the accepted policy owner. Service sessions cannot apply an exception.

1. `POST /orchestration/flows/{flow_id}/evaluation-waiver/preview` with
   `node_id`, `expected_plan_version`, `expected_plan_hash`, `criterion_ids` and
   the owner's `reason`. This is a whole-evaluation waiver; criterion identifiers
   document its scope, not a partial test verdict.
2. Submit the same request to `/evaluation-waiver/accept`, adding the returned
   `snapshot` as `expected_snapshot`.
3. Verify `state: waived` and the attributed `evaluation_waived` decision. The
   normal engine tick releases successors and applies their usual admission,
   concurrency, budget and ownership checks.

The request is bound to the exact accepted plan, evaluation and completed direct
predecessors, including their current PR binding revisions and source heads.
Predecessors must have passed; an exception cannot substitute for missing code.
An existing evaluation contract, any evaluation attempt or execution, an explicit
human evaluation gate, another tenant, an expired policy or a changed preview
refuses acceptance. Lock contention returns a retryable conflict with no partial
write. Identical retries return the original decision.

The exception creates no run, claim, attempt, test evidence, new permission or
budget reservation. It does not rewrite the accepted plan, worker authority or
other evaluation requirements. `evaluation_executed` and `machine_pass_claimed`
are both false in the immutable decision. A changed plan or predecessor binding
invalidates its dependency effect; a raw `waived` state without a valid current
human decision does not release work.

Deploy the gateway, tick and UI support before accepting a waiver. No schema
migration is required: the existing state column stores the shared vocabulary
as text, and the approval uses the existing append-only decision ledger.
