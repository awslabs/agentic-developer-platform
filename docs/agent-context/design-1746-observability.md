# Design Note: Knowledge Layer Observability — Logs, Traces, Metrics

**Issue:** #1746 (Child of EPIC #1345)
**Author:** @agent-architect
**Date:** 2026-06-24
**Status:** Design of record
**Scope:** Module-level observability for the Knowledge Layer: structured logs, distributed traces, and metrics — per-document, per-user, per-tenant. Operator-facing; distinct from the user-facing status UI (E10 #1736).

---

## 1. Executive Summary

The Knowledge Layer (`modules/agent-context/`) today has **no module-wide telemetry pipeline**. State tracking exists (`index_runs`/`index_run_stages` in Postgres), but:

- Logs live in pod stdout via basic `logging.basicConfig()`, with no structured fields
- Subprocess logs (SCIP indexer, Neptune CSV loader, DeepWiki calls) are captured by `subprocess.run(capture_output=True)` and **discarded** after truncation to 500 chars (`sqs-worker.py:235`)
- No distributed traces span the clone→index→store→serve path
- No Knowledge-Layer-specific metrics exist in CloudWatch
- There is no way to answer "what happened to asset X, for user Y, in tenant Z?" without `kubectl logs` spelunking

This design extends the **existing** OTel→ADOT→CloudWatch/X-Ray pipeline (#1630, #1680, #1695) to the Knowledge Layer. It does NOT create a new collector or a new telemetry transport — it instruments the ingestion worker and Door service to emit structured signals to the already-deployed ADOT Collector.

---

## 2. Architecture

```
  agent-context namespace                              adp-agents namespace
  ┌───────────────────────────────────┐  ┌──────────────────────────────────┐
  │                                   │  │                                  │
  │  ┌─────────────────────┐          │  │     ┌───────────────────┐       │
  │  │ ingestion-worker     │──────────┼──┼────►│  ADOT Collector    │       │
  │  │ (KEDA ScaledJob)     │ gRPC:4317│  │     │  (existing #1630)  │       │
  │  └─────────────────────┘          │  │     └───────────────────┘       │
  │                                   │  │         │   │   │               │
  │  ┌─────────────────────┐          │  │         │   │   │               │
  │  │ Door (context-mcp)   │──────────┼──┼────────►│   │   │               │
  │  │ (Deployment)         │ gRPC:4317│  │         │   │   │               │
  │  └─────────────────────┘          │  │         │   │   │               │
  │                                   │  │         │   │   │               │
  └───────────────────────────────────┘  └─────────┼───┼───┼───────────────┘
                                                       │   │   │
                                    ┌──────────────────┘   │   └──────────────┐
                                    ▼                      ▼                   ▼
                           AWS X-Ray              CloudWatch Metrics     CloudWatch Logs
                           (traces/spans)         ns: ADP/KnowledgeLayer  /adp/dev/knowledge-layer/
```

### Why reuse the existing ADOT Collector?

The ADOT Collector in `modules/agent-factory/webhook-ingress/infra/otel-collector.tf` already:
- Receives OTLP on 4317 (gRPC) and 4318 (HTTP) as a ClusterIP service
- Exports traces → X-Ray, metrics → CloudWatch EMF, logs → CloudWatch Logs
- Has a scoped IRSA role with CloudWatch + X-Ray write permissions
- Lives in the `adp-agents` namespace (reachable cross-namespace from the ingestion workers in `agent-context` namespace via FQDN)

Both the ingestion worker (KEDA ScaledJob) and the Door service run in the `agent-context` namespace. They reach the ADOT collector cross-namespace via its cluster-internal FQDN: `adot-collector.adp-agents.svc.cluster.local:4317`. No NetworkPolicy blocks this path today (`agent-context` has no egress restrictions; `adp-agents` has no ingress restrictions on the collector).

**IAM extension needed:** The existing IRSA policy allows writes to the `ADP/AgentTelemetry` CloudWatch namespace. We add `ADP/KnowledgeLayer` to the condition, and add the new log group (`/adp/dev/knowledge-layer/`) to the resource ARN list.

---

## 3. The Correlation Spine

Every log line, trace span, and (cardinality-safe) metric carries a common set of dimensions that enable pivot across pillars:

| Dimension | Type | Source | On Logs | On Spans | On Metrics |
|-----------|------|--------|---------|----------|------------|
| `asset_id` | UUID | `repositories.id` | Yes | Yes | **No** (cardinality) |
| `owner_sub` | UUID | `X-Owner-Sub` header / SQS envelope | Yes | Yes | Yes (bounded) |
| `tenant_id` | VARCHAR | `X-Tenant-Id` header / SQS envelope | Yes | Yes | Yes |
| `project_id` | UUID | `X-Project-Id` header / message tags | Yes | Yes | **No** |
| `run_id` | UUID | `index_runs.id` (from `StageTracker`) | Yes | Yes | **No** |
| `stage` | ENUM | Current pipeline stage (clone/zoekt/scip/...) | Yes | Yes | Yes |
| `asset_type` | ENUM | repo/url/doc/infra | Yes | Yes | Yes |
| `repo_name` | VARCHAR | `org/repo` | Yes | Yes | **No** |

### Cardinality discipline

- **Metrics** are dimensioned by `tenant_id`, `stage`, `asset_type`, `owner_sub` only — all bounded sets. `asset_id` is NOT a metric label (it's per-document = unbounded = CloudWatch cost explosion).
- **Logs and traces** carry the full dimension set including `asset_id`, `run_id`, `repo_name` — these are high-cardinality-safe in log/trace systems.
- The operator pivots: metric anomaly (bounded dimensions) → filter by `tenant_id`/`stage` → find the traces → find the exact asset log lines.

### How the spine is stamped

**Ingestion worker (sqs-worker.py):** The SQS message body already contains `source` (the repo/url), `content_type`, and `tags` (which carry `tenant_id`/`owner_sub` from the E8 #1721 identity bridge). On message receipt, the worker:
1. Extracts correlation dimensions from the message + environment
2. Sets them as OTel resource attributes (via `OTEL_RESOURCE_ATTRIBUTES` env composition — same pattern as #1695 `entrypoint.py:290-314`)
3. Creates a root span with these attributes
4. Passes the `run_id` (from `StageTracker.run_id`) into the span/log context

**Door service:** Identity headers (`X-Owner-Sub`, `X-Tenant-Id`, `X-GitHub-Login`) arrive on every request. The Door stamps them onto the request span and any logs emitted during that request.

---

## 4. Pillar 1: Structured Logs

### 4.1 Current state

- `sqs-worker.py`: `logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s")`
- `ingest-repo.py`: same basic format
- `door/server.py`: `log = logging.getLogger(__name__)` — standard Python logger, no structured output
- Subprocess output: `result.stdout.decode()[:2000]` captured, logged at `log.info("Output: %s", stdout[:500])` — rest discarded

### 4.2 Target state

**JSON structured logs** from all Knowledge Layer components, with the correlation spine baked in:

```json
{
  "timestamp": "2026-06-24T10:15:32.123Z",
  "level": "INFO",
  "logger": "ingest-repo",
  "message": "Stage completed",
  "asset_id": "a1b2c3d4-...",
  "owner_sub": "user-12345",
  "tenant_id": "acme-corp",
  "run_id": "e5f6g7h8-...",
  "stage": "deepwiki",
  "asset_type": "repo",
  "repo_name": "acme-corp/backend",
  "duration_ms": 4521,
  "status": "verified",
  "artifact_ref": "s3://bucket/wikis/acme-corp/backend/wiki.md"
}
```

### 4.3 Implementation: structured logging library

Introduce a `telemetry.py` module in `modules/agent-context/images/ingestion/` (and mirror in `door/`) that:

1. Configures `structlog` (or plain `logging` with a JSON formatter — prefer the lightweight `python-json-logger` to avoid a new dep) with the correlation dimensions as bound context
2. Provides a `CorrelationContext` dataclass populated at message-receipt time
3. Attaches correlation fields to every log record via a custom filter/processor

```python
# modules/agent-context/images/ingestion/telemetry.py

@dataclass
class CorrelationContext:
    asset_id: str | None = None
    owner_sub: str | None = None
    tenant_id: str | None = None
    project_id: str | None = None
    run_id: str | None = None
    stage: str | None = None
    asset_type: str | None = None
    repo_name: str | None = None

# Thread-local (or contextvars) storage for the current context
_ctx: ContextVar[CorrelationContext] = ContextVar("correlation_ctx")
```

### 4.4 Fixing the lost-subprocess-logs gap

**The problem:** `sqs-worker.py:229` runs child scripts with `capture_output=True` and only logs `stdout[:500]`. The child processes (e.g., `ingest-repo.py` which itself calls `scip_indexer.py` subprocess runs) have their own loggers writing to *their* stdout — which is captured by the parent and mostly discarded.

**The fix (two parts):**

1. **Stream subprocess output in real-time** instead of `capture_output=True`:
   ```python
   def _run_subprocess(cmd, timeout, correlation_ctx):
       process = subprocess.Popen(
           cmd,
           stdout=subprocess.PIPE,
           stderr=subprocess.STDOUT,
           text=True,
       )
       for line in process.stdout:
           # Re-emit with parent's correlation context
           log.info(line.rstrip(), extra=correlation_ctx.as_dict())
       process.wait()
   ```

2. **Child scripts output JSON structured logs** (not plain text): When `OTEL_LOG_FORMAT=json` env var is set, child scripts use the same JSON formatter. The parent either:
   - Forwards raw JSON lines to the OTLP log exporter (preferred — preserves child's correlation fields), or
   - Parses and re-emits with merged context

**Design decision:** Option A (forward raw JSON lines as OTLP log records) is cleaner. The child process inherits `OTEL_RESOURCE_ATTRIBUTES` from the parent's environment, so its OTel SDK auto-stamps the correlation dimensions. The parent just needs to stream (not capture) and let the child's own OTLP exporter send to the collector directly. This eliminates the forwarding problem entirely.

**However,** child processes are short-lived — the OTLP batch timeout (5s) may not flush before the child exits. Mitigation: set `OTEL_BSP_SCHEDULE_DELAY=1000` for children, and call `force_flush()` in the child's cleanup. Alternatively, the parent streams lines and forwards to its own exporter (which has the full lifecycle).

**Recommended approach:** Children inherit OTLP env vars, emit directly to the collector. Parent logs "subprocess started/completed/failed" events (with duration + exit code). Between the child's own logs and the parent's bookends, the full picture is captured. If a child crashes before flush, the parent's "failed" event with captured stderr (last 2KB) fills the gap.

### 4.5 Log destination

- **CloudWatch Logs group:** `/adp/<env>/knowledge-layer/ingestion` (worker logs) and `/adp/<env>/knowledge-layer/door` (query logs)
- **Transport:** OTLP → ADOT Collector → `awscloudwatchlogs` exporter
- **Retention:** 30 days (configurable via Terraform variable)
- **Query example:** "Show me everything for asset X":
  ```
  SOURCE '/adp/dev/knowledge-layer/ingestion'
  | filter asset_id = 'a1b2c3d4-...'
  | sort @timestamp asc
  ```

---

## 5. Pillar 2: Distributed Traces

### 5.1 Span tree per asset (ingestion)

```
[ingestion_run]                          ← root span (one per SQS message)
  ├─ [clone]                             ← child span per stage
  │    └─ duration, status, git_url
  ├─ [zoekt_index]
  │    └─ duration, shard_count, artifact_ref
  ├─ [cgc_structural]
  │    └─ duration, symbol_count, artifact_ref
  ├─ [scip_index]
  │    ├─ [scip_python]                  ← per-language sub-span
  │    ├─ [scip_typescript]
  │    └─ [neptune_load]                 ← Neptune bulk-load sub-span
  ├─ [deepwiki]
  │    └─ duration, wiki_size_bytes, artifact_ref
  ├─ [sbom_source]
  │    └─ duration, dep_count, artifact_ref
  └─ [graphrag]
       └─ duration, entity_count, artifact_ref
```

Each span carries:
- The correlation dimensions as span attributes
- `otel.status_code` = OK/ERROR
- Stage-specific attributes (see above)
- Link to `run_id` for back-reference to `index_run_stages` Postgres state

### 5.2 Span per Door query (serving)

```
[door_query]                             ← root span per /call or /mcp request
  ├─ verb: search | understand | impact | browse | remember | experience
  ├─ [acl_check]                         ← ACL evaluation sub-span
  ├─ [backend_call]                      ← backend-specific sub-span
  │    ├─ [zoekt_search]                 (for search verb)
  │    ├─ [s3_fetch]                     (for understand/browse)
  │    └─ [neptune_query]                (for impact)
  └─ result_count, filtered_count, duration_ms
```

### 5.3 Implementation

**Library:** `opentelemetry-api` + `opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-grpc` (Python).

**Ingestion worker instrumentation:**

Wrap `StageTracker.stage()` context manager to also open/close a trace span:

```python
# In stage_tracker.py (or a new telemetry_stage_tracker.py wrapper)
from opentelemetry import trace

tracer = trace.get_tracer("knowledge-layer.ingestion")

@contextmanager
def stage(self, stage_name: str):
    with tracer.start_as_current_span(
        stage_name,
        attributes={
            "asset_id": self._repo_id,
            "run_id": self._run_id,
            "repo_name": self._repo,
            "stage": stage_name,
        },
    ) as span:
        ctx = StageContext(_stage_name=stage_name)
        # ... existing logic ...
        if ctx._verified:
            span.set_status(trace.StatusCode.OK)
            span.set_attribute("artifact_ref", ctx._artifact_ref)
        else:
            span.set_status(trace.StatusCode.ERROR, ctx._error or "unverified")
        yield ctx
```

**Door instrumentation:**

FastAPI auto-instrumentation via `opentelemetry-instrumentation-fastapi` creates a span per request. We add a custom SpanProcessor or middleware that stamps correlation dimensions from request headers onto the span.

### 5.4 Trace destination

- **AWS X-Ray** via ADOT's `awsxray` exporter
- Traces are queryable by: `annotation.asset_id`, `annotation.tenant_id`, `annotation.run_id`
- X-Ray Service Map shows: `ingestion-worker → S3 / Neptune / DeepWiki` and `Door → Zoekt / S3 / Neptune`

### 5.5 Cross-service correlation

The ingestion worker and Door don't call each other in a single request (ingestion is async, serving is sync). They correlate via `asset_id` + `run_id`:
- An operator sees a Door query return stale data → checks the asset's trace → finds the last ingestion run → sees which stage failed

This is **correlation by shared dimension**, not trace context propagation. Both traces carry `asset_id` as an annotation, enabling X-Ray filter: `annotation.asset_id = "X"` returns both the ingestion trace and all serving traces for that asset.

---

## 6. Pillar 3: Metrics

### 6.1 Metric definitions

**CloudWatch namespace:** `ADP/KnowledgeLayer`

| Metric | Dimensions | Unit | Description |
|--------|-----------|------|-------------|
| `AssetsRegistered` | `tenant_id`, `asset_type` | Count | New assets added to catalog |
| `AssetsQueued` | `tenant_id`, `asset_type` | Count | Messages sent to SQS |
| `AssetsIndexed` | `tenant_id`, `asset_type`, `stage` | Count | Stage completions (verified) |
| `AssetsFailed` | `tenant_id`, `asset_type`, `stage` | Count | Stage failures |
| `StageLatency` | `tenant_id`, `stage` | Milliseconds | Per-stage p50/p95/p99 |
| `IngestionDuration` | `tenant_id`, `asset_type` | Milliseconds | Full run (all stages) |
| `QueueDepth` | — | Count | SQS visible messages |
| `WorkerConcurrency` | — | Count | Active KEDA pods |
| `DoorQueryRate` | `tenant_id`, `verb` | Count/sec | Query throughput by verb |
| `DoorQueryLatency` | `tenant_id`, `verb` | Milliseconds | Query p50/p95/p99 |
| `DoorQueryErrors` | `tenant_id`, `verb` | Count | 5xx responses |

### 6.2 Emission mechanism

Two paths:
1. **OTel Metrics API** in the worker/Door code → ADOT Collector → `awsemf` exporter → CloudWatch. This is the primary path.
2. **SQS metrics** (QueueDepth) come from CloudWatch's built-in SQS metrics — no emission needed, just dashboard consumption.

### 6.3 Knowledge Layer Dashboard

**Name:** `adp-<env>-knowledge-layer` (Terraform-managed, sibling to `adp-<env>-agent-observability`)

**Rows:**

| Row | Left widget | Right widget |
|-----|-------------|--------------|
| 1 | **Pipeline funnel** (registered→queued→indexed→failed) stacked area | **Ingestion throughput** over time (by tenant) |
| 2 | **Per-stage success rate** bar chart (last 24h) | **Per-stage latency** (p50/p95 line chart) |
| 3 | **Failure breakdown** by stage + error type | **Per-tenant volume** (assets indexed by tenant) |
| 4 | **Door query rate** by verb (stacked area) | **Door query latency** p95 by verb |
| 5 | **Queue health** (SQS visible + in-flight + DLQ) | **Worker concurrency** vs queue depth |
| 6 | **Active alerts** text widget | **Per-run detail** table (Logs Insights query) |

### 6.4 Alerts (CloudWatch Alarms)

| Alarm | Condition | Severity | Action |
|-------|-----------|----------|--------|
| `KL-RollupNotPromoting` | `AssetsIndexed` = 0 for 30min while `AssetsQueued` > 0 | P2 | SNS → ops |
| `KL-StageFailing` | `AssetsFailed` > 10 in 5min for any single `stage` | P2 | SNS → ops |
| `KL-ZombieRuns` | `index_runs` with `status='running'` and `started_at` > 1h ago (Logs Insights alarm) | P3 | SNS → ops |
| `KL-SQSBacklogHigh` | `QueueDepth` > 200 (KEDA cap) for 15min | P3 | SNS → ops |
| `KL-DoorErrorSpike` | `DoorQueryErrors` > 5/min for 5min | P2 | SNS → ops |
| `KL-DeepWikiFailRate` | `AssetsFailed` where `stage=deepwiki` > 50% of `AssetsIndexed` where `stage=deepwiki` over 1h | P3 | SNS → ops |

---

## 7. Fail-Open Discipline

**Non-negotiable:** Telemetry emission MUST NOT block or crash the ingestion pipeline. The #1695 lesson (identity enrichment): analysis-first, fail-open.

### Implementation pattern

```python
def emit_metric_safe(metric_name: str, value: float, dimensions: dict):
    """Emit a CloudWatch metric, swallowing any error."""
    try:
        meter.create_counter(metric_name).add(value, attributes=dimensions)
    except Exception:
        # Log at DEBUG only — do not re-raise, do not block
        log.debug("Telemetry emission failed for %s", metric_name, exc_info=True)
```

**Rules:**
1. All OTel SDK calls wrapped in try/except (never bare)
2. Telemetry errors logged at DEBUG (not WARNING — avoids log spam drowning real signals)
3. If the ADOT collector is unreachable, the SDK's batch exporter drops data silently (by design)
4. Feature flag: `KNOWLEDGE_LAYER_TELEMETRY_ENABLED` env var (default `true`). Set `false` to disable all instrumentation without code change
5. **Regression test:** A test that patches the OTel SDK to raise on every call, then runs a full ingestion cycle — must complete successfully with all stages verified

### Startup resilience

If the OTel SDK fails to initialize (e.g., collector DNS unresolvable), the worker MUST still function:
```python
try:
    _init_otel()
except Exception:
    log.warning("OTel initialization failed — running without telemetry")
    # Set global flag; all emit_* calls become no-ops
```

---

## 8. Tenant Boundary on Telemetry

### Access control

- CloudWatch Logs groups are operator-scoped (IAM-based access)
- Dashboard widgets filter by `tenant_id` — an operator who has access to the dashboard sees all tenants
- **No per-tenant dashboards in v1** (operator tool, not tenant-facing). If tenant-facing observability is needed later, it would be a separate surface with IAM-scoped log access.

### Data minimization

- Logs carry `tenant_id` and `repo_name` but NOT file contents, code snippets, or secrets
- Trace span attributes carry structural metadata (durations, counts, refs) — never content
- The subprocess log forwarding strips any credential patterns (reuse `_sanitize_git_output` from `ingest-repo.py:207-219`)

### Cross-tenant leakage prevention

- `repo_name` is tenant-scoped data — it appears in logs/traces but NOT in globally-visible metric dimensions
- Dashboard Logs Insights queries always include `tenant_id` in the filter when showing per-tenant views
- X-Ray trace access is IAM-scoped (same boundary as CloudWatch)

---

## 9. Integration with Existing State Tracking

The `index_runs` / `index_run_stages` tables (migration `003`) remain the **transactional state of record**. This EPIC adds the **observability lens** that correlates by `run_id`:

```
                    ┌───────────────────────────────────┐
                    │      Postgres (state of record)   │
                    │                                   │
                    │  index_runs (id = run_id)         │
                    │  index_run_stages (run_id FK)     │
                    │                                   │
                    └───────────┬───────────────────────┘
                                │
                         correlate by run_id
                                │
    ┌───────────────────────────┼────────────────────────────┐
    │                           │                            │
    ▼                           ▼                            ▼
 CloudWatch Logs          X-Ray Traces              CloudWatch Metrics
 (full detail per event)  (span tree per run)       (aggregated counters)
 filter: run_id=X         annotation: run_id=X      N/A (run_id not a dimension)
```

**No schema changes needed.** The existing tables have all necessary columns. The `run_id` produced by `StageTracker.__init__` → `db.create_index_run()` is the join key. We stamp it onto telemetry; we do NOT add telemetry columns to Postgres.

---

## 10. Reuse Table

| What | From Where | How We Use It |
|------|-----------|---------------|
| ADOT Collector (K8s Deployment + Service) | `modules/agent-factory/webhook-ingress/infra/otel-collector.tf` (#1630) | Extend IAM policy to allow `ADP/KnowledgeLayer` namespace + new log group |
| Dashboard Terraform pattern | `agent-observability-dashboard.tf` (#1680) | Mirror for Knowledge Layer dashboard |
| Identity enrichment (OTEL_RESOURCE_ATTRIBUTES) | `entrypoint.py:290-314` (#1695) | Same pattern for ingestion worker env composition |
| `StageTracker` + `db.py` (run_id, stages) | `modules/agent-context/images/ingestion/` (#1423) | Correlation spine — run_id links telemetry to state |
| Scoped IRSA role pattern | `otel-collector.tf:288-371` | Extend (not duplicate) the existing role |
| Ops-centre dashboard concept | `docs/tagging-and-observability.md` (#1709-1711) | Sibling dashboard for the Knowledge Layer |
| Tenant/owner_sub scope dimensions | `design-1721-tenant-isolation.md` (#1721) | Stamp onto all telemetry signals |
| Project scope dimension | `design-1728-project-scoping.md` (#1728) | Stamp onto logs/traces (not metrics) |

---

## 11. Feature Flagging

| Flag | Default | Scope |
|------|---------|-------|
| `KNOWLEDGE_LAYER_TELEMETRY_ENABLED` | `true` | Master kill switch — disables all emission |
| `KNOWLEDGE_LAYER_TRACES_ENABLED` | `true` | Disable traces only (logs+metrics still work) |
| `KNOWLEDGE_LAYER_METRICS_ENABLED` | `true` | Disable metrics only |
| `enable_knowledge_layer_otel` | `false` (Terraform) | Deploys the dashboard + alarms + log groups |

The Terraform flag gates infrastructure (dashboard, alarms, log group creation). The env vars gate runtime emission. This allows shipping instrumented code before the infra is provisioned, and disabling emission per-env without a redeploy.

---

## 12. File-Level Changes

### New files

| Path | Purpose |
|------|---------|
| `modules/agent-context/images/ingestion/telemetry.py` | Correlation context, structured logger setup, OTel init, fail-open helpers |
| `modules/agent-context/door/telemetry.py` | Door-side OTel init, request span enrichment middleware |
| `modules/agent-context/terraform/modules/observability/main.tf` | CloudWatch dashboard + alarms + log groups |
| `modules/agent-context/terraform/modules/observability/variables.tf` | Module variables |
| `modules/agent-context/terraform/modules/observability/outputs.tf` | Dashboard ARN, log group names |
| `modules/agent-context/tests/unit/test_telemetry_failopen.py` | Regression test: telemetry failure doesn't block ingestion |

### Modified files

| Path | Change |
|------|--------|
| `modules/agent-context/images/ingestion/sqs-worker.py` | Import telemetry; init OTel on startup; stream subprocess output; stamp correlation context |
| `modules/agent-context/images/ingestion/ingest-repo.py` | Use structured logger; emit stage spans |
| `modules/agent-context/images/ingestion/stage_tracker.py` | Wrap stage() with trace spans; emit metrics on verify/fail |
| `modules/agent-context/images/ingestion/scip_indexer.py` | Use structured logger; child OTel env inheritance |
| `modules/agent-context/door/server.py` | Add OTel middleware; stamp request spans with identity headers |
| `modules/agent-context/terraform/main.tf` | Add `module "observability"` call |
| `modules/agent-factory/webhook-ingress/infra/otel-collector.tf` | Extend IAM policy (new CW namespace + log group) |
| `modules/agent-context/pyproject.toml` | Add OTel SDK dependencies |

---

## 13. Dependencies & Sequencing

| Dependency | Status | Impact on this EPIC |
|-----------|--------|---------------------|
| #1630 ADOT Collector | Closed (shipped) | Reuse directly |
| #1680 Agent dashboard | Closed (shipped) | Pattern to follow |
| #1695 Identity enrichment | Closed (shipped) | Pattern to follow |
| #1721 Tenant isolation | Design of record | Provides `tenant_id`/`owner_sub` on SQS messages |
| #1728 Project scoping | Design of record | Provides `project_id` |
| `enable_agent_otel` flag | `true` (enabled in `terraform.tfvars`) | Required for telemetry to flow — already enabled |

**Sequencing:** The instrumentation code can ship before #1721/#1728 are fully implemented — dimensions are nullable and the code stamps whatever is available (fail-open on missing dimensions).

---

## 14. Deployment

### Automatic on merge
- `agent-context-deploy.yml` rebuilds the ingestion worker image and Door image (picks up new instrumentation code)
- No separate CI needed for the telemetry module (it's part of the images)

### Manual follow-ups
1. `terraform apply` on `modules/agent-context/terraform/` (creates dashboard, alarms, log groups)
2. `terraform apply` on `modules/agent-factory/webhook-ingress/infra/` (extends ADOT IAM policy)
3. Set `enable_knowledge_layer_otel = true` in the agent-context terraform.tfvars
4. Set `enable_agent_otel = true` in webhook-ingress terraform.tfvars (if not already enabled)
5. Verify ADOT collector pod is healthy: `kubectl get pods -n adp-agents -l app.kubernetes.io/name=adot-collector`

### Rollback
- Set `KNOWLEDGE_LAYER_TELEMETRY_ENABLED=false` in the ingestion worker / Door deployment env → immediate silence
- Revert the Terraform apply for dashboard/alarms (no data loss — just removes the views)
- The instrumented code with telemetry disabled has zero overhead (no-op SDK paths)

---

## 15. Validation

### Per-story acceptance (see child stories below)
Each story has its own validation criteria.

### EPIC-level acceptance
1. **Three-pillar correlation:** For a given `asset_id`, retrieve: the full structured log trail, the ingestion span tree (X-Ray), and the dashboard metrics — all showing the same asset's journey.
2. **Tenant filter:** Filter the dashboard by `tenant_id` → only that tenant's assets/volume visible.
3. **User filter:** Filter logs/traces by `owner_sub` → only that user's assets visible.
4. **Failed stage visibility:** Deliberately fail a stage (e.g., set `DEEPWIKI_URL` to invalid) → failed span in X-Ray + error log with correlation + `AssetsFailed` metric incremented + alarm fires.
5. **Fail-open regression:** Patch OTel SDK to throw on every call → full ingestion cycle completes with all stages verified (test_telemetry_failopen.py).
6. **Lost-subprocess-logs fixed:** Run a SCIP indexing job → the SCIP subprocess output appears in CloudWatch Logs with the parent's correlation context.
7. **No cross-tenant leakage:** Two tenants index repos simultaneously → each tenant's dashboard/log view shows only their own data.

---

## 16. Child Story Decomposition

### Story 1: Telemetry foundation — correlation context + structured logging

**Scope:** Create `telemetry.py` module, configure structured JSON logging, define `CorrelationContext`, wire into `sqs-worker.py` message receipt path. No OTel SDK yet — just structured logs with correlation fields to stdout (which Container Insights already ships to CloudWatch).

**Why first:** Everything else builds on the correlation context being available. Structured logs with the right fields are immediately useful even before the OTLP exporter is wired.

**Acceptance:** Ingestion worker logs are JSON-structured with all available correlation dimensions. A CloudWatch Logs Insights query `| filter asset_id = 'X'` returns the full log trail for that asset.

---

### Story 2: Fix lost-subprocess-logs gap

**Scope:** Change `_run_subprocess` in `sqs-worker.py` from `capture_output=True` to streaming. Ensure child processes (ingest-repo.py, scip_indexer.py) inherit structured logging config and OTel env vars. Forward child output to parent's log stream with correlation context.

**Why second:** The biggest debuggability gap today. Once structured logging exists (Story 1), we can immediately make subprocess logs visible.

**Acceptance:** SCIP indexer subprocess output, Neptune CSV loader output, and DeepWiki call logs all appear in the parent's CloudWatch log stream with the correct `run_id`/`asset_id` correlation.

---

### Story 3: Distributed traces — ingestion span tree

**Scope:** Add `opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-grpc` to the ingestion image. Create a span tree per indexing run (root span = run, child spans = stages). Instrument `StageTracker.stage()` to emit spans. Configure OTLP export to the existing ADOT collector endpoint.

**Why third:** Requires Story 1 (correlation context) to stamp spans correctly. The span tree is the most operator-valuable new signal for debugging stuck/slow indexing.

**Acceptance:** After a full ingestion run, X-Ray shows the span tree with per-stage duration and status. Query by `annotation.run_id` or `annotation.asset_id` returns the tree.

---

### Story 4: Distributed traces — Door query spans

**Scope:** Add OTel instrumentation to the Door FastAPI app. Auto-instrument with `opentelemetry-instrumentation-fastapi`. Add middleware that stamps identity headers (`X-Owner-Sub`, `X-Tenant-Id`) onto the request span. Add sub-spans for backend calls (Zoekt, S3, Neptune).

**Why fourth:** Independent from ingestion (can parallelize with Story 3 if desired). Completes the serve-side tracing.

**Acceptance:** A Door `/call` request produces an X-Ray trace with verb, identity, backend call duration, and result count.

---

### Story 5: Metrics emission + ADOT IAM extension

**Scope:** Define OTel metrics (counters + histograms) in the ingestion worker and Door. Emit `AssetsIndexed`, `AssetsFailed`, `StageLatency`, `DoorQueryRate`, `DoorQueryLatency`. Extend the ADOT collector IAM policy to allow the `ADP/KnowledgeLayer` CloudWatch namespace and the new log groups.

**Why fifth:** Requires Stories 1+3 (correlation context + OTel SDK wired). Metrics are the high-level health signal that the dashboard (Story 6) consumes.

**Acceptance:** CloudWatch custom metrics appear in namespace `ADP/KnowledgeLayer` with correct dimensions. Metrics increment on indexing and on Door queries.

---

### Story 6: Knowledge Layer CloudWatch dashboard + alarms

**Scope:** Create Terraform module `modules/agent-context/terraform/modules/observability/` with the CloudWatch dashboard (6 rows, per §6.3) and alarms (per §6.4). Follow the `agent-observability-dashboard.tf` pattern (Logs Insights query widgets + metric widgets).

**Why sixth:** Requires Stories 5 (metrics flowing) to show useful data. The dashboard is the operator's primary interaction surface.

**Acceptance:** Dashboard `adp-dev-knowledge-layer` visible in CloudWatch with all 6 rows populated. Alarms fire when conditions are met (testable by forcing a failure burst).

---

### Story 7: Fail-open regression test + telemetry kill switch

**Scope:** Write `test_telemetry_failopen.py` that patches the OTel SDK to raise exceptions, then runs a simulated ingestion cycle (unit test, not e2e). Verify all stages complete. Verify the `KNOWLEDGE_LAYER_TELEMETRY_ENABLED=false` flag fully disables emission. Document the kill switch in the module README.

**Why last:** Validates the safety property that underpins the whole design. Requires the instrumentation (Stories 1-5) to exist so it can test that failures are handled.

**Acceptance:** Test passes in CI. Toggling the flag to `false` produces zero OTel SDK calls (verified by mock).

---

## 17. Open Questions

1. **ADOT Collector namespace sharing vs dedicated instance:** The agent-factory ADOT collector is in `adp-agents`. The Door runs in `agent-context` namespace. Cross-namespace ClusterIP access works (just use FQDN), but should we deploy a second collector in `agent-context` for isolation? **Recommendation: NO — reuse the single collector.** It's stateless and handles the volume trivially. Revisit if throughput demands it.

2. **Log retention per-tenant:** Should different tenants have different retention? **Recommendation: NO in v1.** Uniform 30-day retention. Tenant-specific retention requires per-tenant log groups (complexity not warranted now).

3. **Subprocess forking strategy (Story 2):** Should children export OTLP directly (simpler code, flush risk) or should the parent forward (more reliable, more coupling)? **Recommendation: Children export directly + parent logs bookend events.** The ADOT collector handles concurrent producers.
