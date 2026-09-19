# Persona-to-model rollout evidence harness (PMM-09)

This directory contains the non-enforcing half of #5427. It turns the approved
25-cell live matrix into a machine-checked evidence contract, validates the
report-only shadow baseline per dispatch path, and binds results to exact
deployment artifacts and health checks.

It deliberately cannot invoke a model, spend money, deploy, enable the Agent
Models UI, or change the model-policy posture. The validator rejects evidence
that claims enforcement was authorized, probing was enabled, or a non-zero
probe ceiling existed. This keeps a pull-request run from becoming an
enforcement or spend boundary by accident.

## Local use

```bash
cd platform/evals/persona-model-rollout
python3 rollout_gate.py validate-manifest
python3 rollout_gate.py template > /tmp/pmm09-evidence.json
python3 rollout_gate.py shadow-report /tmp/worker-events.jsonl > /tmp/shadow.json
python3 rollout_gate.py assess /tmp/pmm09-evidence.json
python3 -m unittest discover -s tests -v
```

The template begins with all cells `not_run` and intentionally lacks deployment
and source evidence. Assessment exits nonzero until that context is supplied;
an otherwise valid report may remain incomplete with cells not run. A passed invocation cell requires a provider request ID, response
token counts, a matching `usage_logs` row, cost, an agent run ID, and a hash of
real model output. A passed refusal requires the fixed reason code to reach the
requester plus queried proof of zero usage rows and zero provider invocations.
Configuration changes require both an API response ID and audit-row ID.

L22 is evaluated separately from admission refusals. Every required path needs
the configured minimum number of distinct launch observations. An unconfigured
principal must match the legacy model unless an explicit direct choice explains
the difference. A saved mapping may equal the legacy model; equality is not a
failure. The actual SDK model must remain the legacy selection. Refusals in the shadow ledger fail validation
because report-only posture never relaxes an admission gate.

The SDK emits one-line `PMM09_MODEL_SHADOW` JSON events after verifying the fresh
report-only decision for each launch or retry. These contain canonical owner
facts from the signed decision, the unchanged SDK model and the proposed model,
live posture revision and the nonce that joins PMM-08 accounting. Worker dispatch
labels come from its envelope environment; chat and ARC identify their SDK paths.
Startup telemetry is not counted. Missing proposals or unidentified paths remain
incomplete. An SDK admission event is selection evidence, not a provider receipt. `shadow-report` maps those events onto the approved
path vocabulary. Unknown paths are rejected explicitly, so new dispatch paths
cannot disappear from coverage by accident. Paste its output into the evidence
document's `shadow_comparison` member before assessment.

## Live boundary still outstanding

The eventual operator-owned run must first satisfy the canonical deployment
guide, confirm the `embark1` target is account `879318057152`, record the
deployment state and health checks, obtain an explicit spend ceiling, and clear
the PMM-06/PMM-07 authority and requester-feedback gates. It must run against an
exact merged revision of every predecessor. None of those approvals is implied
by this harness or by merging its pull request.

## Evidence contract details

Use `evidence_schema_revision: "1.1"` and an exact 40-character `source_revision`
matching `deployment.git_revision`. Image and deploy-state digests must be valid
SHA-256 identifiers. The validator checks recorded evidence; it does not contact
the deployment, query a provider, authenticate uploaded logs or independently
verify that an operator's artifact IDs are true. Retain the source artifacts for
review. Synthetic fixtures in `tests/` are never live acceptance evidence.

Each passed cell has the matrix's exact `surface`, UTC timestamp, account/region
matching the deployment, and a canonical `principal_kind` (`human` or
`service_account`) plus nonempty owner and tenant IDs. Invocation evidence has
finite nonnegative cost, integer token counts, a real-output hash and separate
provider, usage-row and run IDs. L1/L2 require three distinct personas/models and
three complete distinct calls in `invocations`; L2 must match L1's owner/mappings.

L17/L18/L21 use `chain` with `chain_id`, `root_invocation_id`, `snapshot_digest`,
`root_principal_kind`, `root_principal_id` and ordered `hops`. Every hop carries
its own invocation facts, persona/model, canonical tenant/owner, chain/digest and
`parent_invocation_id` (null only at root). Runs and provider/usage IDs must be
distinct. L21 requires an `explicit-direct` root and every descendant must have
`has_direct_override: false`, an independent mapping/default source and a
different resolved model.

L22 requires explicit `rejected_events: []`; rejected, duplicate or malformed
observations fail assessment. Each observation retains `model_decision_id`,
`invocation_id`, `attempt`, `phase: "sdk_admission"`, `policy_status: "proposed"`,
`posture_verified: true`, live `runtime_posture: "report_only"`, snapshot/policy/
posture revisions, canonical owner, and actual/legacy/proposed model fields.

L23/L24 are structurally unpassable here. `complete` and `enforcement_ready`
always remain false. This PR completes non-enforcing tooling; deployment,
paid live acceptance, UI enablement, enforcement and rollback certification
remain operator-owned work under #5427. Existing webhook deployment holds remain.
