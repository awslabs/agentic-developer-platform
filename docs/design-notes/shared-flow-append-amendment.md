# Append missing producers to an accepted shared flow

The human who owns an accepted `code_only` shared-worker policy can add story
producers and dependency edges through:

- `POST /api/orchestration/flows/{flow_id}/append/preview`
- `POST /api/orchestration/flows/{flow_id}/append/accept`

Both routes require the existing authenticated-human `PLAN_APPROVE` permission.
The request contains `expected_plan_version`, `expected_plan_hash`, `added_nodes`,
`added_edges`, and `reason`. Acceptance also requires the exact `expected_snapshot`
returned by preview. No policy, principal, allowance, existing-node replacement,
or worker-identity fields are accepted.

The server copies the accepted document and validates the complete resulting
graph with the authoritative proposal validator. New nodes must be stories in
existing waves; an added edge must touch a new node. Existing nodes and edges
remain present. New incoming dependencies can target only pending/ready nodes
that have never started. Existing node IDs, states, attempts, bindings, receipts,
and execution history remain unchanged; new nodes start pending for normal tick
admission.

Acceptance serializes with dispatch using the flow lock, refreshes its graph,
and requires a settled boundary: no running/awaiting-merge nodes, unfinished
executions, unresolved actions, unverified worker reports, or authoring jobs.
Claims retained by a concluded cycle are preserved only when their exact worker
report and generation are settled. Expired leases are never evidence of exit.
External issue ownership still requires the normal dispatch claim checks and the
operator's live evidence checks; this endpoint cannot take over another run.

The new version copies the execution policy and continuation marker unchanged,
including original acceptance time, expiry, limits, principal, historical-spend
baseline, and meter identity. It never initializes or resets the meter. A lost
response may be retried after new workers start; the saved request/snapshot and
current resulting plan must still match, and the retry writes nothing.

Evaluation attachments are bound to their original accepted plan. Preview lists
contracts needing explicit reacceptance after the append; their decision history
is retained and their authority is not carried forward implicitly. Generic full
amendments refuse shared continuations rather than discarding the marker.
