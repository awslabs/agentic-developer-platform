# Design Note: ADOT Collector for Agent SDK Telemetry

**Issue:** #1630
**Author:** @agent-architect
**Date:** 2026-06-19
**Status:** Implementation-ready design
**Scope:** ADOT Collector install + ScaledJob env wiring + IRSA + entrypoint OTEL_RESOURCE_ATTRIBUTES composition. Content-level unmasking (prompts/tool I/O) deferred.

---

## 1. Executive Summary

The Claude Agent SDK emits OpenTelemetry spans (traces), metrics (tokens/cost), and
logs automatically when configured via environment variables. ADP agent-worker pods
currently have no collector to receive this data. This design adds a minimal ADOT
Collector Deployment to the `adp-agents` namespace with three pipelines routing data
to CloudWatch (metrics + logs) and X-Ray (traces), filling the application + business
observability lenses for Agent Factory.

---

## 2. Architecture

```
                   adp-agents namespace
  ┌──────────────────────────────────────────────────────────┐
  │                                                          │
  │  ┌─────────────┐   gRPC :4317   ┌───────────────────┐   │
  │  │ agent-worker │──────────────►│  ADOT Collector    │   │
  │  │  (ScaledJob) │   HTTP :4318  │  (Deployment 1/1)  │   │
  │  └─────────────┘               └───────────────────┘   │
  │                                    │   │   │            │
  └────────────────────────────────────┼───┼───┼────────────┘
                                       │   │   │
                        ┌──────────────┘   │   └──────────────┐
                        ▼                  ▼                   ▼
               CloudWatch X-Ray    CloudWatch Metrics    CloudWatch Logs
               (awsxray exporter)  (awsemf exporter)    (awscloudwatchlogs)
                                   ns: ADP/AgentTelemetry
```

### Why module-local Deployment (not EKS managed addon)?

- The `amazon-cloudwatch-observability` EKS addon deploys a DaemonSet for container
  metrics — it does NOT include an OTLP receiver for application telemetry.
- A dedicated Deployment lets us pin to the `adp-agents` namespace, customize the
  config via ConfigMap, scope IRSA tightly, and gate independently.
- Follows the same Kubernetes provider pattern as the KEDA Helm release in this module.
- Can migrate to a platform-level addon later if needed.

---

## 3. Collector Pipelines

### 3.1 Traces (application lens)

Expected spans from the SDK: `claude_code.interaction` (root), `llm_request`,
`tool` (one per tool invocation). Exported to X-Ray via the `awsxray` exporter.

### 3.2 Metrics (business lens)

Expected metrics: `claude_code.tokens` (input/output), `claude_code.cost`,
`claude_code.session_count`. Exported via `awsemf` to CloudWatch custom metrics
namespace `ADP/AgentTelemetry`. Dimensioned by `service.name`, `tenant.id`,
`enduser.id`, `agent.persona`.

### 3.3 Logs

Structural logs (no content — see Non-goals) exported to CloudWatch Logs via
`awscloudwatchlogs`.

---

## 4. Ephemeral Pod Considerations

ScaledJob pods exit when the task completes. Mitigations:

1. **Batch timeout: 5s** — frequent enough for most runs (>30s) without creating
   excessive pressure on the collector. The SDK's Node.js runtime flushes pending
   spans via `forceFlush()` during graceful shutdown (`SIGTERM` → `beforeExit`).
2. **`OTEL_BSP_SCHEDULE_DELAY=5000`** — BatchSpanProcessor delay matching collector
   batch timeout.
3. **`OTEL_BSP_EXPORT_TIMEOUT=10000`** — 10s export timeout within the pod's 30s
   `terminationGracePeriodSeconds`.
4. **1s batch timeout rejected** — creates unnecessary collector pressure; the real
   risk is SIGKILL (which nothing can mitigate anyway), not normal exits.

---

## 5. IRSA Scope (follows #1204 discipline)

The collector service account gets a minimal IAM role:

- **X-Ray**: `PutTraceSegments`, `PutTelemetryRecords`, `GetSamplingRules`,
  `GetSamplingTargets`, `GetSamplingStatisticSummaries` — Resource `*` (service
  limitation; cannot be scoped).
- **CloudWatch Metrics**: `PutMetricData` — Resource `*` with **condition**
  `cloudwatch:namespace = "ADP/AgentTelemetry"` (IAM condition key scopes to our
  namespace only).
- **CloudWatch Logs**: `CreateLogGroup`, `CreateLogStream`, `PutLogEvents`,
  `DescribeLogGroups`, `DescribeLogStreams`, `PutRetentionPolicy` — scoped to
  `arn:aws:logs:<region>:<account>:log-group:/adp/dev/agent-factory/otel/*`.

---

## 6. ScaledJob Env Vars (gated by `enable_agent_otel`)

Static env vars set by the ScaledJob template:
```
CLAUDE_CODE_ENABLE_TELEMETRY=1
OTEL_TRACES_EXPORTER=otlp
OTEL_METRICS_EXPORTER=otlp
OTEL_LOGS_EXPORTER=otlp
OTEL_EXPORTER_OTLP_PROTOCOL=grpc
OTEL_EXPORTER_OTLP_ENDPOINT=http://adot-collector.adp-agents.svc.cluster.local:4317
OTEL_SERVICE_NAME=adp-agent-worker
OTEL_RESOURCE_ATTRIBUTES=service.namespace=adp-agents,deployment.environment=${env}
OTEL_BSP_SCHEDULE_DELAY=5000
OTEL_BSP_EXPORT_TIMEOUT=10000
OTEL_METRIC_EXPORT_INTERVAL=5000
ENABLE_AGENT_OTEL=1
```

Dynamic attributes appended by `entrypoint.py` at runtime (from SQS envelope):
```
OTEL_RESOURCE_ATTRIBUTES += tenant.id=${tenant_id},agent.persona=${persona},enduser.id=${user_id},session.id=${correlation_id}
```

Content unmasking flags (`OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_TOOL_DETAILS`,
`OTEL_LOG_TOOL_CONTENT`) are explicitly NOT set — deferred pending data-governance
decision.

---

## 7. NetworkPolicy

The egress NetworkPolicy in `scaledjob-netpol.tf` has an additional rule allowing
agent-worker pods to reach the collector on port 4317 (gRPC) via pod selector
`app.kubernetes.io/name=adot-collector`.

---

## 8. Feature Flag

`var.enable_agent_otel` (bool, default `false`). When false:
- Collector Deployment/Service/ConfigMap are not created (`count = 0`)
- IRSA role is not created (`count = 0`)
- ScaledJob env block has no OTEL vars (local.otel_env_block = "")
- entrypoint.py OTEL_RESOURCE_ATTRIBUTES composition is skipped

Turning it on requires only `enable_agent_otel = true` in tfvars + `terraform apply`
\+ an agent-worker image rebuild (for the entrypoint.py change).

---

## 9. Validation Runbook

1. Set `enable_agent_otel = true`, apply.
2. Verify collector pod: `kubectl get pods -n adp-agents -l app.kubernetes.io/name=adot-collector`
3. Check collector health: `kubectl exec -n adp-agents <pod> -- wget -qO- http://localhost:13133/`
4. Trigger an agent task.
5. Check X-Ray: trace with `claude_code.interaction` root span.
6. Check CloudWatch metrics: namespace `ADP/AgentTelemetry`, metric `claude_code.tokens`.
7. Check CloudWatch logs: log group `/adp/dev/agent-factory/otel/logs`.
8. Kill collector → confirm agent task still completes (non-blocking).
9. Verify IRSA: collector SA cannot write to any other CW namespace or log group.
