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

The template begins with all cells `not_run`. That is valid and honestly
incomplete. A passed invocation cell requires a provider request ID, response
token counts, a matching `usage_logs` row, cost, an agent run ID, and a hash of
real model output. A passed refusal requires the fixed reason code to reach the
requester plus queried proof of zero usage rows and zero provider invocations.
Configuration changes require both an API response ID and audit-row ID.

L22 is evaluated separately from admission refusals. Every required path needs
the configured minimum observation count. An unconfigured principal must match
the consolidated legacy default; a configured principal must produce the
expected selection difference. Refusals in the shadow ledger fail validation
because report-only posture never relaxes an admission gate.

Report-only workers emit one-line `PMM09_MODEL_SHADOW` JSON events containing
the signed policy facts, the unchanged legacy choice, and trusted dispatch
channel/trigger fields. `shadow-report` maps those events onto the approved
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
