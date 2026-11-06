# Settle an exited failed reviewer before replanning

An exited reviewer can leave its story running, its review execution blocked,
and its work claim held. An owner can record that verified failure through the
existing review-recovery API using `mode: "settle_failed"`. This concludes the
execution, releases its claim with reason `failed`, and marks the story `failed`.
It does not dispatch a worker, approve the PR, reset attempts or renew a budget.

Use an authenticated human session with `PLAN_APPROVE` in the flow's tenant.
The caller must also match the accepted execution policy's owner. Read the
current node attempt, accepted plan version, claim's reviewer run and current
GitHub PR head before requesting a preview:

```http
POST /orchestration/nodes/{node_id}/review-recovery/preview
Content-Type: application/json
```

```json
{
  "mode": "settle_failed",
  "expected_attempt": 5,
  "expected_plan_version": 1,
  "expected_run_id": "current-reviewer-run-id",
  "expected_head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "reason": "Record verified reviewer failure before simplifying the remaining work."
}
```

Review the returned execution, claim, PR head and `retired_admission_evidence`.
The latter lists any obsolete, actionless earlier admissions and each original
worker's protected terminal outcome. Every such worker must have positively
exited, with a lower claim generation and no handoff or effects. An active,
unverified or changed predecessor blocks the entire operation.

Copy the preview's `snapshot` to `expected_snapshot` in the same request body,
then submit it to:

```http
POST /orchestration/nodes/{node_id}/review-recovery/accept
```

Acceptance commits the current failure and any verified historical admission
retirements together. The failed reviewer and older workers' protected records,
PR binding, attempts, spend and action history remain intact. The human audit
records their evidence digests. Replaying the identical accepted request returns
`created: false` and the same decision ID.

A newer PR head is allowed when the request matches GitHub's current head;
settlement does not endorse that code or change the historical review binding.
Merged or queued PRs, active workers, unknown effects and changed snapshots are
refused. A busy or stale response requires rereading state and obtaining a fresh
preview. Expired execution windows and exhausted budget or attempt allowances do
not prevent this operation: it stops already exited work and authorizes no new
work.

After acceptance, verify the story is failed, its current execution is concluded,
and its claim is released. The amendment impact check can now evaluate a settled
boundary; unrelated active work can still prevent amendment. Use the ordinary
plan amendment process to simplify remaining stories and their dependencies.
Resume or authorize further work separately through existing flow controls,
including their current budget, attempt, deployment and evaluation requirements.
Settlement itself does not resume the flow.
