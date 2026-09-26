# Recover an engine node or a historical PR association

Use the same installed `adp` client and tenant as your flow. These commands use
canonical orchestration controls. They cannot approve gates, grant credentials,
change budgets or start a second scheduler.

```bash
adp flow show FLOW_ID --json
adp flow node resume NODE_ID --flow FLOW_ID --reason 'Dependency repaired' --dry-run --json
adp flow node resume NODE_ID --flow FLOW_ID --reason 'Dependency repaired' \
  --expect-revision PREVIEW_REVISION --operation-id UUID --yes --json
adp flow decisions FLOW_ID --json
adp flow watch FLOW_ID --once --json
```

The snapshot endpoint is `GET /orchestration/nodes/{node_id}/recovery?flow_id=...`.
It binds the tenant, flow, node, attempt, state and current PR association to a
revision. `POST /orchestration/nodes/{node_id}/resume` takes `reason`, optional
`reconciled` (default false), `expected_flow_id` and `expected_revision`. Only
failed, halted, rejected-at-gate and awaiting-merge nodes can resume. A stale
revision conflicts before effects. The node lock serializes recovery with
canonical node/PR writers. Explicit `--reconciled` attests that an old owner's
outstanding effects and credentials were reconciled; `--yes` never supplies that
attestation. Existing human approval authority remains required.

A private JSON request file for PR recovery contains:

```json
{
  "repo": "OWNER/REPOSITORY",
  "pr_number": 123,
  "head_sha": "0123456789abcdef0123456789abcdef01234567",
  "reason": "This reviewed pull request delivered the historical story."
}
```

```bash
chmod 600 REQUEST.json
adp flow recover-pr FLOW_ID --node NODE_ID --request-file REQUEST.json --dry-run --json
adp flow recover-pr FLOW_ID --node NODE_ID --request-file REQUEST.json \
  --expect-revision PREVIEW_REVISION --operation-id UUID --yes --json
```

The client requires the full reviewed SHA. The existing server schema additionally
accepts positive `provider_repository_id`, exact `provider_pr_node_id`, an
explicit `replaces_reason` (10–2000 characters), and `adopt_delivery` for the
canonical never-dispatched historical-adoption path. GitHub verifies PR identity;
the client cannot declare checks green or a story complete. The response retains
`remaining_hold` when review, merge, gates or reconciliation remain outstanding.

Every delivery attempt records a private local receipt before sending. Reusing
that UUID with the same input reads the current canonical state and never sends
another mutation. Different inputs under the same UUID conflict. An interrupted,
refused or uncertain request reports `pending`; reconcile `flow decisions` and
`flow show`. Keep the receipt across disconnects. This is local at-most-once
submission, not a new server-side idempotency ledger. The server revision fence
also refuses a changed node if the local receipt is lost.

`--dry-run` takes precedence over `--yes`. It reads only. Older gateways lacking
the recovery snapshot contract refuse without falling back to unfenced writes.
`configured` means the recovery acknowledgement was received; only observed
execution proves continuation.

E37 runs flow listing and malformed recovery-target refusal in the existing
nightly EC2 regression. Full #5630 acceptance additionally needs an owned blocked
flow, the #5331 inception and #5329 amendment journeys, exact human decisions,
verified engine/worker readiness, independently enforced inference limits and
cleanup evidence. E37 alone does not satisfy those live criteria.
