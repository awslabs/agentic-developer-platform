# Dependency amendments while a shared flow is paused

An owner can now correct explicit dependency edges into unstarted waves without
waiting for unrelated active stories to merge. The flow must use the accepted
shared-worker, `code_only` continuation and must be paused at acceptance.

This operation edits dependencies only. Node addresses, membership, definitions,
evaluation requirements, shared contracts and policy documents remain identical.
It does not replace a plan wholesale, add/remove stories, or waive dependencies.

## Eligibility and preservation

A wave is identified by both `epic_ref` and `wave_ref`. Any execution, report,
PR binding, dispatch decision, historical non-pending state, claim or continuation
attempt freezes its entire wave, including siblings that have not run. Accepted
evaluation contracts and waivers also freeze their waves. Resetting a counter
or returning a node to pending cannot reopen a wave.

Every added or removed edge must target an eligible wave. An edge can originate
in a frozen wave; that changes a future wave's prerequisites. The resulting graph
must pass the normal proposal validator, including cycle detection. An affected
ready node returns to pending for normal readiness calculation on the next tick.

Acceptance creates a new accepted-plan version and an immutable human decision.
The original versions remain available. Active workers retain their exact run,
execution, claim, credential, attempt and original plan-version identities.
Readers verify the amendment chain, both document hashes, unchanged non-edge
fields and frozen-wave prerequisites before accepting an older assignment.
Ordinary plan replacements and append amendments do not receive this exemption.
The chain is bounded to 100 consecutive dependency amendments.

The same verification preserves exact evaluation waiver/contract decisions and
budget, concurrency, retry and expiry supplements. It does not issue replacement
approvals, renew authority, reset spend, or turn a waiver into a machine PASS.
Waiver predecessor/binding checks, membership, claim generation, permissions and
expiry checks continue to apply. Active non-story executions are refused by this
first, code-delivery-only amendment path.

Pause stops admission; it does not stop existing workers or reconciliation.
Acceptance locks the flow/current plan, takes node locks with NOWAIT to avoid
dispatch's reverse lock order, and rechecks eligibility and the preview snapshot.
Concurrent resume or dispatch cannot bypass the gate. Ordinary progress inside
already frozen waves can finish without invalidating the preview.

## API

Both endpoints require an authenticated human with `PLAN_APPROVE` who owns the
existing execution policy:

```text
POST /orchestration/flows/{flow_id}/wave-dependencies/preview
POST /orchestration/flows/{flow_id}/wave-dependencies/accept
```

Example preview body (use the current version/hash and real graph addresses):

```json
{
  "expected_plan_version": 7,
  "expected_plan_hash": "<current 64-character plan hash>",
  "added_edges": [
    {"from_address": "flow/epic/validation-v2/T5", "to_address": "flow/epic/validation-v3/T7"}
  ],
  "removed_edges": [],
  "reason": "Record the prerequisite for the unstarted validation wave"
}
```

Preview writes nothing and returns the changed/frozen waves, explicit edge diff,
preserved policy hash and evaluation decision IDs, original acceptance time,
expiry, next version/hash, and `snapshot`. Submit the same body to accept with
`expected_snapshot` set to that token. Acceptance is atomic and idempotent; a
lost-response retry does not reset nodes, even if the flow has since resumed.

A concurrent node lock returns retryable `amendment_dispatch_in_progress` (409).
Changed eligibility or plan/snapshot returns a conflict requiring a fresh preview.
Acceptance leaves the flow paused. Resume is a separate owner operation.

Tests cover durable-history freezing, protected siblings, DAG validation,
rollback/idempotency, PostgreSQL pause/dispatch lock races, active reviewer model
and report authorization, fresh review dispatch, merge reconciliation while
paused, unchanged V0 waiver and policy supplements, current progress display,
watchdog timing, review recovery before/after amendment and later deadline renewal.
