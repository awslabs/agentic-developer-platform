# Learnings: Issue #1746 — Architecture Review of Knowledge Layer Observability EPIC

**Date:** 2026-06-24
**Agent:** @agent-architect
**Issue:** #1746
**PR:** #1750

## Key Findings

### 1. Namespace topology is critical for OTel instrumentation planning
The ingestion worker (KEDA ScaledJob) runs in `agent-context` namespace, NOT `adp-agents`. The ADOT collector lives in `adp-agents`. This means cross-namespace FQDN access is required: `adot-collector.adp-agents.svc.cluster.local:4317`. The design note originally stated same-namespace placement — a factual error that would have misled downstream implementers.

**How to verify:** `config.env` in the module root sets `NAMESPACE="agent-context"`. The ScaledJob manifest at `manifests/ingestion-scaledjob.yaml` uses `${NAMESPACE}`.

### 2. NetworkPolicy state matters for observability wiring
- `adp-agents` namespace has a default-deny EGRESS policy (`scaledjob-netpol.tf:16-31`) with explicit allows for the agent-scaledjob pods.
- `adp-agents` has NO ingress policy — any pod can send traffic TO it.
- `agent-context` namespace has NO NetworkPolicy at all (wide open).
- Result: ingestion worker and Door CAN reach the ADOT collector today. BUT if someone adds a NetworkPolicy to `agent-context` later, they'd need an explicit egress rule to port 4317 on the ADOT collector.

### 3. IAM condition blocks new CloudWatch namespaces
The ADOT collector's IRSA role policy has `"cloudwatch:namespace" = "ADP/AgentTelemetry"` as a condition on `PutMetricData`. Any new metric namespace (e.g. `ADP/KnowledgeLayer`) will be silently rejected until this condition is extended to a list. The log group ARN is similarly scoped.

**File:** `modules/agent-factory/webhook-ingress/infra/otel-collector.tf:337`

### 4. `enable_agent_otel` is already TRUE
Despite comments claiming it's `false`, the `terraform.tfvars` in `modules/agent-factory/webhook-ingress/infra/` sets `enable_agent_otel = true`. The ADOT collector is live and ready to receive telemetry. This means Stories 3+ can wire OTLP export without any Terraform flag changes for the collector itself — just the IAM policy extension.

### 5. The `python-json-logger` dependency pattern
The gateway pins `python-json-logger==2.0.7` and uses it directly. The ingestion image now also pins `2.0.7` but implements its own JSON formatter via stdlib `json.dumps`. The library is installed but unused in `telemetry.py`. This is harmless but adds a maintenance surface. v3.x of `python-json-logger` has breaking changes — stay on 2.0.7 if it's ever imported.

### 6. Subprocess log gap root cause
`sqs-worker.py:226` uses `subprocess.run(cmd, capture_output=True, ...)` which captures all stdout/stderr into memory and only logs `stdout[:500]`. SCIP, Neptune CSV loader, and DeepWiki subprocess output is lost. The fix (Story 2) is to switch to `subprocess.Popen` with line-by-line streaming. Child processes should inherit OTel env vars for direct-to-collector export.

### 7. Correlation context timing
`run_id` is only available AFTER `StageTracker.__init__` calls `db.create_index_run()`. Any logs emitted between SQS message receipt and StageTracker creation will have `run_id=None`. This is acceptable but worth documenting — operators querying by `run_id` won't find the initial "Processing started" log line.

### 8. Design note as source of truth
The 620-line design note at `docs/agent-context/design-1746-observability.md` is the authoritative reference for all 7 stories. It includes:
- Complete metric definitions (§6.1)
- Dashboard row layout (§6.3)
- Alarm definitions with thresholds (§6.4)
- Feature flag matrix (§11)
- File-level change list (§12)

Future implementers should read this FIRST, not the issue body.

## What Worked
- Running `grep` on the actual Terraform files to verify IAM conditions caught the namespace confusion early.
- Cross-referencing `config.env` with the design note's architecture claims revealed the factual error.
- The test suite was well-written and immediately runnable (after installing deps).

## What Didn't Work
- The previous agent's implementation comment (Comment 6) claimed `enable_agent_otel = false` without checking the tfvars file. Always verify claims against the actual committed code.

## Recommendations for Future Agents
1. When working on observability, always verify the namespace topology by reading `config.env` or the K8s manifests — don't trust design docs.
2. Before claiming a feature flag is on/off, read the actual `.tfvars` file that gets applied (not just `variables.tf` defaults).
3. The ADOT collector's IAM policy is the chokepoint for new telemetry — any new CloudWatch namespace or log group requires an IAM policy update in `modules/agent-factory/webhook-ingress/infra/`.
