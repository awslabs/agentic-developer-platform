# Adopt a reviewed maintenance repair on the original delivery story

An operations story can remain pinned to an old deployment workflow after its
maintenance repair has been merged. Restarting the story alone cannot approve a
new workflow or deployment role. This recovery accepts an exact maintenance
merge through a versioned plan amendment while retaining the original story.

Use `POST /orchestration/flows/{flow_id}/maintenance-adoption/preview`, followed by
the same request plus its returned `expected_snapshot` at
`POST /orchestration/flows/{flow_id}/maintenance-adoption/accept`.

The caller must be an authenticated human platform administrator with
`PLAN_APPROVE`, matching the original execution-policy principal. Current
membership, policy expiry, and elapsed-time ceiling must still authorize work.
The node must be failed or ready, and all of its previous executions, effects,
and work claims must be settled. A ready, never-dispatched node is supported.

The request identifies:

- `node_id`, `expected_plan_version`, `expected_plan_hash`, `expected_attempt`;
- `pull_number`, exact `reviewed_head`, and its `source_revision` merge commit;
- the exact `workflow_sha256`, complete `reviewed_paths`, and `required_checks`;
- the explicitly reviewed `job_mutation_role_arn` in the existing account;
- `review_ref` and `reason`, attributing the owner's review judgment.

GitHub independently proves the repository identity, two-parent merge and tested
head, required checks, changed paths, workflow bytes, and default-branch ancestry.
The merge must contain every accepted predecessor, the previous effective source,
and **every** historically published repair head, including heads superseded
before their PR merged. `review_ref` records the evidence the owner reviewed; it
does not claim that GitHub issued an approval that GitHub did not issue.

Acceptance creates a `maintenance_release_adopted` human decision and a new
ordinary accepted-plan version. Only the maintenance reference, source and
definition revisions, and explicitly reviewed job role change. The target,
workflow inputs, verification checks, cleanup requirement, predecessors, persona,
and execution-policy authority stay exact. The original workflow receipts and
failed reports remain queryable. No `operations_repair_merge` receipt is invented.

Acceptance does **not** resume the flow, dispatch a worker, increase retry limits,
extend expiry, or reset attempts, elapsed time, spend, task history, or node IDs.
Use the existing owner retry/window controls if a separately reviewed bounded
increase is needed. Then use the normal resume control. Worker admission records
the adopted source before constructing the workspace. Later repairs still need
their own native merge receipts; old publications are reconciled only by the
verified adoption checkpoint.

If a published repair was squash-merged, explicitly include
`squashed_repair_history` entries with `pull_number`, `reviewed_head`,
`merge_revision`, and the original `published_heads`. Each entry must match the
original publication receipts. GitHub must prove that every declared publication
is an ancestor of that PR's final head, that the real one-parent squash merge and
final head have identical Git trees, and that the squash merge is an ancestor of
the maintenance source. Required checks must pass on both the historical repair
head and the maintenance head. The original baseline and effective source still
require direct ancestry. The receipt retains this distinct provenance and every
original publication; it does not invent a native repair-merge action. Unequal
trees, rewritten heads, ambiguous mappings, and missing publications are refused.

This initial recovery supports generic delivery contracts and one maintenance
adoption per story. Further adoption attempts fail explicitly. Squashed history
without the explicit proof above, and other rewritten history, fail verification
rather than silently discarding published repairs. No browser automation is involved.
