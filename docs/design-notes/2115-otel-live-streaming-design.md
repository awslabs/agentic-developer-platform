# Design Note: Live Agent OTel Span Streaming + Per-Run Activity Detail Page

**Issue:** #2115
**Author:** @agent-architect
**Date:** 2026-06-26
**Status:** Design spike — hardened recommendations for child-story decomposition
**Scope:** Agent-worker OTel span model, live push channel to browser, per-run Activity detail page, trace backend strategy, bounds/sampling.

---

## 1. Executive Summary

Agents currently stream per-turn activity to a GitHub Check Run (`CheckRunStreamer`) and produce SDK-native OTel telemetry (shipped in #1630). This design adds **two new capabilities** off the existing instrumentation:

1. **Custom application-level OTel spans** — one root span per invocation with child spans per turn and grandchild spans per tool call, carrying ADP-specific attributes (persona, tenant, cost, tokens). These flow through the existing ADOT Collector to X-Ray for durable post-hoc trace waterfalls.

2. **Near-real-time event push** — the same per-turn events are tee'd to an **SSE endpoint** on the gateway, enabling a new **Activity Detail Page** that shows an agent working live and a completed-run timeline afterward.

---

## 2. Design Decisions (resolving the 6 open questions)

### 2.1 Backend Choice: AWS X-Ray (managed) — RECOMMENDED

| Factor | X-Ray (managed) | Self-hosted Tempo/Jaeger |
|--------|-----------------|--------------------------|
| Ops burden | Zero — AWS manages storage, scaling, retention | New stateful workload (storage, upgrades, monitoring) |
| Ingestion lag | 1-5 seconds (acceptable for post-hoc view) | <1s with direct write |
| Custom attributes | Full support via `aws.xray.annotations` and metadata | Native OTel attributes |
| Cost | ~$5/million traces (our volume: ~500 traces/day = pennies) | EBS/S3 storage + compute |
| Query UX | CloudWatch ServiceLens / X-Ray Analytics / raw API | Grafana Tempo / Jaeger UI |
| Already deployed | ADOT Collector + X-Ray exporter live (#1630), `enable_agent_otel=true` | Nothing |
| Retention | Configurable (default 30 days, extend to 90) | Must manage |

**Decision: X-Ray.** The infrastructure is already live. The 1-5s ingestion lag doesn't matter because the live-push channel handles real-time visibility (separate transport). X-Ray is the *durable query store*, not the live feed. Self-hosting Tempo/Jaeger adds an ops surface with no proportional benefit at our scale.

**Action:** Enable `enable_xray_tracing=true` at the platform level (`platform/infra/main.tf:132`) so the gateway service role also gets X-Ray write permissions (currently disabled). The ADOT Collector already exports to X-Ray.

### 2.2 Live Channel: SSE from Gateway — RECOMMENDED

| Factor | Extend chat WebSocket API GW | New SSE endpoint on gateway |
|--------|------------------------------|------------------------------|
| Reuse | WebSocket infra deployed (`adp-dev-gateway-ws`) | Gateway already serves the Activity API (FastAPI + auth) |
| Auth model | Lambda authorizer (Cognito) | Same JWT auth as all gateway endpoints (FastAPI `Depends(get_current_user)`) |
| Connection model | Persistent bi-directional (overkill: client only reads) | Unidirectional server → client (natural fit) |
| Complexity | Must add new route/action type, parse in Lambda, route to correct worker | One FastAPI streaming endpoint with `StreamingResponse` |
| Scaling | API GW WebSocket has 500 concurrent connections/account soft limit | Gateway pod handles SSE; scales with pod replicas |
| Tenant scoping | Must authorize per-message in Lambda | Natural: endpoint resolves `user_id` from JWT, queries only their events |
| Frontend | Must share `useAgentChat` hook logic or fork | Clean new `useRunActivity` hook with `EventSource` API |
| Push source | Worker must know the WebSocket connection ID | Worker pushes to a shared SQS/Redis channel; gateway relays |

**Decision: SSE from the gateway.** Reasons:
1. The Activity detail page is a **read-only** feed (operator watches; no upstream messages). SSE is the correct primitive for unidirectional server-push.
2. The gateway already has JWT-authenticated FastAPI endpoints for the Activity layer, tenant-scoped queries, and the `InvocationDetail` modal data — extending it with an SSE endpoint is minimal code.
3. The WebSocket API GW is purpose-built for the chat widget (bi-directional conversation). Overloading it with a different concern (observability feed) violates separation of concerns and complicates the Lambda routing.
4. `EventSource` API is natively supported in all browsers with automatic reconnection — simpler client code than WebSocket frame parsing.

**Endpoint shape:**
```
GET /me/agent-invocations/{invocation_id}/stream
Accept: text/event-stream
Authorization: Bearer <jwt>

→ SSE events (per turn/tool):
data: {"type":"turn_start","turn":1,"timestamp":"..."}
data: {"type":"tool_call","turn":1,"tool":"Bash","input_preview":"npm test","timestamp":"..."}
data: {"type":"turn_end","turn":1,"text_preview":"Tests pass...","cost_usd":0.02,"timestamp":"..."}
data: {"type":"run_complete","total_turns":5,"total_cost_usd":0.12,"duration_ms":45000}
```

### 2.3 Where the Live Tee Happens: Worker-direct to Redis Pub/Sub — RECOMMENDED

Three options evaluated:

| Option | Pros | Cons |
|--------|------|------|
| Worker → ADOT → streaming sink → gateway | Single export path | ADOT has no streaming sink exporter; would need custom processor; adds latency + complexity |
| Worker → gateway HTTP push | Simple | Tight coupling; worker needs gateway auth; adds HTTP overhead per event |
| Worker → Redis Pub/Sub → gateway SSE | Decoupled; Redis already in-cluster (ElastiCache); pub/sub is fire-and-forget | Worker needs Redis client; one new dependency |

**Decision: Worker pushes events to Redis Pub/Sub; gateway subscribes and relays via SSE.**

Architecture:
```
Agent Worker pod                     Gateway pod
     │                                    │
     │ ──── Redis PUBLISH ────►           │
     │   channel: "run:{invocation_id}"   │ ← SSE /stream endpoint subscribes
     │                                    │    to "run:{invocation_id}" on
     │                                    │    client connect, relays events
     │ ──── OTLP gRPC ────►              │
     │   (unchanged: ADOT → X-Ray)        │
```

Why Redis:
- **Already deployed** — `adp-dev-redis` (ElastiCache) is used by the gateway for session/cache. The agent-worker ScaledJob pod has network access to it (same VPC, SG allows).
- **Pub/sub is ephemeral** — no persistence needed; if no subscriber is connected when an event publishes, it's simply dropped (acceptable: the post-hoc view uses X-Ray traces, not the live channel).
- **Minimal worker change** — the worker already has `REDIS_HOST` available (or can get it from SSM). One new lightweight publish call per turn, fire-and-forget.
- **Natural tenant isolation** — channel name includes `invocation_id`; the gateway SSE endpoint only subscribes to channels for invocations the authenticated user owns (verified via DynamoDB lookup before subscribing).

**IAM/Network:** The ScaledJob pod already has network egress to ElastiCache (verified: gateway uses the same VPC subnets). No new IAM grant needed — Redis is network-authenticated (AUTH token in Secrets Manager at `adp/dev/redis-auth-token`).

### 2.4 Span Model + Attributes

#### Trace structure (per invocation):

```
Root Span: "agent-invocation"
  ├── Span: "turn.1"
  │     ├── Span: "tool.Bash" (command preview)
  │     ├── Span: "tool.Read" (file path)
  │     └── Span: "tool.Edit" (file path)
  ├── Span: "turn.2"
  │     └── Span: "tool.Grep" (pattern)
  └── Span: "turn.3"
        └── Span: "tool.Write" (file path)
```

#### Span attributes:

| Span level | Attribute | Value | OTel semantic |
|------------|-----------|-------|---------------|
| Root | `session.id` | `{correlation_id}` | Already set by #1630 entrypoint.py |
| Root | `adp.invocation_id` | `{event_id}` | Custom annotation |
| Root | `adp.tenant_id` | `{tenant_id}` | Maps to `tenant.id` resource attr |
| Root | `adp.persona` | `developer\|architect\|...` | Maps to `agent.persona` resource attr |
| Root | `adp.repo` | `owner/repo` | Custom |
| Root | `adp.issue_number` | `123` | Custom |
| Root | `enduser.id` | `{user_id}` | OTel semantic |
| Root | `adp.model` | `claude-sonnet-4-20250514` | Custom |
| Turn | `adp.turn_number` | `1` | Custom |
| Turn | `adp.turn_cost_usd` | `0.023` | Custom (set on span end) |
| Turn | `adp.turn_tokens` | `1500` | Custom (set on span end) |
| Turn | `adp.text_preview` | First 200 chars | Custom (non-sensitive structural) |
| Tool | `adp.tool_name` | `Bash` | Custom |
| Tool | `adp.tool_input_preview` | `npm test` (120 chars) | Custom |
| Tool | `adp.tool_result_status` | `success\|error` | Custom |

#### Trace ID ↔ invocation_id relationship:

The OTel trace ID is a 128-bit random value (X-Ray format: `1-{timestamp}-{random}`). It does NOT equal `invocation_id`. The linkage is:
- **Forward lookup** (invocation → trace): query X-Ray with filter `annotation.adp_invocation_id = "{event_id}"`.
- **Reverse lookup** (trace → invocation): read `adp.invocation_id` annotation from the root span.

The `session.id` resource attribute (set to `correlation_id` by entrypoint.py #1630) groups all traces from a chain under one X-Ray "session" — this is already live.

### 2.5 Concrete Bounds

| Dimension | Bound | Rationale |
|-----------|-------|-----------|
| **Max spans per run** | 500 | 100 turns × 5 tools = 500. Capped in instrumentation — after 500 spans, further tool calls are logged but not spanned. |
| **Span TTL (X-Ray retention)** | 30 days (extend to 90 via AWS config) | Default X-Ray; sufficient for debugging. Older runs → CloudWatch logs only. |
| **Sampling** | 100% (no sampling) for custom spans | At ~500 traces/day × 5 spans avg = 2,500 spans/day. X-Ray free tier covers 100k traces/month. No sampling needed at this scale. Revisit at >10k traces/day. |
| **Live channel: max subscribers per run** | 5 | One operator watching + margin. Gateway enforces at SSE subscribe time. |
| **Live channel: max concurrent SSE connections per pod** | 100 | Each is a lightweight async generator. Pod has headroom; this prevents abuse. |
| **Live channel: event TTL in Redis** | 0 (pub/sub is fire-and-forget) | No persistence in the channel. Missed events → client fetches on reconnect. |
| **Live channel: reconnect strategy** | Client uses `EventSource` auto-reconnect with `Last-Event-ID` header. Gateway replays from in-memory ring buffer (last 50 events per run). | Handles network blips without full page reload. |
| **Event payload size** | Max 4 KB per SSE event | `text_preview` capped at 200 chars; `input_preview` at 120 chars. |

### 2.6 Completed-Run Path: Hybrid (snapshot + query fallback)

| Strategy | Pros | Cons |
|----------|------|------|
| Query X-Ray live | Always fresh; single source of truth | Ingestion lag means incomplete trace for ~5s after run end; X-Ray query API has rate limits |
| Snapshot final trace to DDB/S3 | Fast, no external query; works offline | Stale if reprocessed; extra write path |
| Hybrid: snapshot + fallback | Best of both | Slightly more complex |

**Decision: Hybrid.** On run completion:
1. The worker publishes a final `run_complete` event to Redis with a summary payload (total turns, cost, duration, tool counts).
2. The gateway's SSE endpoint receives this and writes a **run summary snapshot** to the existing `webhook-events` DynamoDB item (new attribute: `run_activity_summary`). This is a lightweight JSON blob (~2 KB) with the turn/tool timeline.
3. The Activity Detail Page:
   - **While `in_progress`**: subscribes to SSE (live feed).
   - **After completion**: reads the `run_activity_summary` from the DynamoDB item (fast, always available), with a "View full trace in X-Ray" link for deep inspection.
   - **Fallback**: if `run_activity_summary` is empty (pre-feature runs or write failure), shows a degraded view (just the existing metadata: status, timing, cost, error) with the X-Ray link.

This avoids depending on X-Ray query latency for the detail page and provides a self-contained per-run activity record.

---

## 3. Architecture Diagram

```
                    ┌─────────────────────────────────────────────────────────┐
                    │               Agent Worker Pod (ScaledJob)               │
                    │                                                         │
                    │  agent-worker.ts message loop                           │
                    │       │                                                 │
                    │       ├── CheckRunStreamer (existing: → GitHub Check)   │
                    │       │                                                 │
                    │       ├── OTelStreamer (NEW)                            │
                    │       │     ├── emit spans (root/turn/tool)            │
                    │       │     │     → OTLP gRPC → ADOT → X-Ray          │
                    │       │     │                                          │
                    │       │     └── publish events (per turn/tool)         │
                    │       │           → Redis PUBLISH "run:{inv_id}"       │
                    │       │                                                 │
                    └───────┼─────────────────────────────────────────────────┘
                            │
          ┌─────────────────┼──────────────────────┐
          │                 │                      │
          ▼                 ▼                      ▼
  ┌──────────────┐  ┌──────────────┐    ┌──────────────────────┐
  │ ADOT Collector│  │  ElastiCache │    │  GitHub Check Run    │
  │ (existing)    │  │  Redis       │    │  (existing)          │
  │    │          │  │  (existing)  │    └──────────────────────┘
  │    ▼          │  │              │
  │  X-Ray       │  │   PUB/SUB    │
  │  (traces)    │  │   channels   │
  │              │  │      │       │
  └──────────────┘  └──────┼───────┘
                           │
                           ▼
              ┌─────────────────────────┐
              │   Gateway Pod (FastAPI)  │
              │                         │
              │  GET /me/agent-invocations/{id}/stream  (SSE)
              │    ├── auth: JWT (same as Activity API)
              │    ├── tenant scope: verify invocation ownership
              │    ├── subscribe to Redis "run:{inv_id}"
              │    └── relay events as SSE data frames
              │                         │
              │  GET /me/agent-invocations/{id}  (existing detail)
              │    └── returns run_activity_summary from DDB
              │                         │
              └─────────────────────────┘
                           │
                           ▼
              ┌─────────────────────────┐
              │   Frontend (React SPA)   │
              │                         │
              │  Activity Detail Page    │
              │    ├── in_progress: EventSource → live feed
              │    ├── complete: render run_activity_summary
              │    └── link: "View trace in X-Ray" (deep link)
              │                         │
              └─────────────────────────┘
```

---

## 4. Reuse Table (what exists, what's new)

| Capability | Exists? | Location | How this EPIC uses it |
|------------|---------|----------|----------------------|
| Per-turn event capture | Yes | `checkRunStreamer.ts` (lines 98-136) | Tee the same `onTurn`/`onToolProgress`/`onResult` calls to the new `OTelStreamer` |
| OTel SDK init (agent-worker) | Yes | #1630 env vars in `scaledjob.tf` (lines 62-94) | SDK auto-creates `claude_code.*` spans; we ADD custom `agent-invocation/turn/tool` spans |
| ADOT Collector + X-Ray export | Yes | `otel-collector.tf` + `awsxray` exporter | No change; our custom spans flow through the same pipeline |
| Redis (ElastiCache) | Yes | `adp-dev-redis`; gateway uses for sessions | Worker publishes events; gateway subscribes for SSE relay |
| Activity detail endpoint | Yes | `GET /me/agent-invocations/{invocation_id}` | Extend response with `run_activity_summary` field |
| DynamoDB webhook-events table | Yes | `adp-dev-webhook-events` with user/tenant/correlation GSIs | Add `run_activity_summary` attribute to existing items |
| JWT auth on Activity routes | Yes | `src/activity/routes.py` | SSE endpoint uses same `get_current_user` dependency |
| InvocationDetail modal | Yes | `InvocationDetail.tsx` | Refactor into a full page (route: `/activity/{invocation_id}`) |
| WebSocket API GW | Yes | `adp-dev-gateway-ws` | NOT reused (chat-specific); SSE is a better fit |
| `invocation_id` ↔ `correlation_id` lineage | Yes | `entrypoint.py`, DDB item attributes | OTel spans annotated with `adp.invocation_id` for X-Ray lookup |

---

## 5. Data Model Changes

### 5.1 DynamoDB: `webhook-events` table — new attribute

| Attribute | Type | Description |
|-----------|------|-------------|
| `run_activity_summary` | Map (JSON) | Per-turn activity snapshot written on run completion |

**Schema of `run_activity_summary`:**
```json
{
  "version": 1,
  "total_turns": 5,
  "total_cost_usd": 0.12,
  "total_tokens": 15000,
  "duration_ms": 45000,
  "model": "claude-sonnet-4-20250514",
  "turns": [
    {
      "turn": 1,
      "started_at": "2026-06-26T11:23:15Z",
      "ended_at": "2026-06-26T11:23:18Z",
      "cost_usd": 0.02,
      "text_preview": "I'll start by reading the issue...",
      "tools": [
        {"name": "Read", "input_preview": "/work/repo/src/main.py", "duration_ms": 120},
        {"name": "Bash", "input_preview": "npm test", "duration_ms": 3400}
      ]
    }
  ],
  "xray_trace_id": "1-66fd1234-abcdef1234567890abcdef12"
}
```

**Size bound:** Max 400 KB DynamoDB item limit. At 500 spans × ~200 bytes/tool = ~100 KB worst case. Safe.

### 5.2 Gateway API: Extended detail response

The existing `GET /me/agent-invocations/{invocation_id}` response gains:
```json
{
  // ...existing fields...
  "run_activity_summary": { /* see above */ },
  "xray_trace_url": "https://console.aws.amazon.com/xray/home?region=us-east-1#/traces/1-..."
}
```

### 5.3 New SSE endpoint

```
GET /me/agent-invocations/{invocation_id}/stream
GET /admin/agent-invocations/{invocation_id}/stream
```

Response: `Content-Type: text/event-stream`

Event types:
```
event: turn_start
id: 1
data: {"turn":1,"timestamp":"2026-06-26T11:23:15Z"}

event: tool_call
id: 2
data: {"turn":1,"tool":"Bash","input_preview":"npm test","timestamp":"2026-06-26T11:23:16Z"}

event: tool_result
id: 3
data: {"turn":1,"tool":"Bash","status":"success","duration_ms":3400,"timestamp":"2026-06-26T11:23:19Z"}

event: turn_end
id: 4
data: {"turn":1,"text_preview":"Tests pass...","cost_usd":0.02,"tokens":1500,"timestamp":"2026-06-26T11:23:20Z"}

event: run_complete
id: 5
data: {"total_turns":5,"total_cost_usd":0.12,"duration_ms":45000,"xray_trace_id":"1-..."}
```

**Reconnect:** If client reconnects with `Last-Event-ID: 3`, gateway replays events 4+ from its ring buffer (last 50 events per active run). If the run is already complete, returns a single `run_complete` event and closes the stream.

---

## 6. Security / Tenant Isolation

| Surface | Enforcement |
|---------|-------------|
| SSE endpoint | `get_current_user` → resolve `canonical_user_id` → verify the `invocation_id` belongs to this user (DDB lookup via `get_invocation(invocation_id, user_id=...)`) before subscribing to Redis channel. Returns 404 if not owned. |
| Admin SSE | `check_permission(USAGE_READ)` + tenant scoping (same as `get_admin_invocation_detail`) |
| Redis channel name | Contains `invocation_id` (UUID) — not guessable, but auth check is the real gate |
| X-Ray trace link | Deep link to AWS Console — inherits IAM console permissions (operators only) |
| Span content | No prompt content, no tool output content in spans. Only structural metadata (tool names, file paths, durations, previews capped at 200 chars). Content unmasking flags remain deferred (#1630 decision). |
| Worker → Redis | Network-level (VPC SG) + Redis AUTH token |

---

## 7. Failure Modes and Degradation

| Failure | Impact | Mitigation |
|---------|--------|------------|
| Redis down | Live SSE stops; runs continue unaffected | SSE endpoint returns 503; frontend falls back to polling detail endpoint every 5s |
| ADOT Collector down | Custom spans lost; runs continue | Already fail-open by design (#1630). `run_activity_summary` still written (separate path) |
| X-Ray ingestion delayed | Trace link shows incomplete for ~5s after run | Detail page uses `run_activity_summary` (not X-Ray query) for the timeline. X-Ray link is a bonus deep-dive. |
| Worker crashes mid-run | Final `run_complete` event not published | Run status transitions to `failed` (existing mechanism); detail page shows partial timeline from last `run_activity_summary` update or "in progress → failed" |
| Gateway pod restart during SSE | Client's EventSource auto-reconnects | Ring buffer lost; replays from DDB summary if run is now complete |
| DDB write throttled on `run_activity_summary` | Summary not persisted | Retry once; if still fails, log warning. Detail page degrades to metadata-only view. |

---

## 8. File-Level Changes (per child story)

### Story A: OTelStreamer component (agent-worker)
- **New:** `modules/agent-factory/agent/src/components/otelStreamer.ts`
- **Modify:** `modules/agent-factory/agent/src/agent-worker.ts` (~line 1091-1112, 1207-1283) — wire `OTelStreamer` alongside `CheckRunStreamer`
- **Modify:** `modules/agent-factory/agent/package.json` — add `@opentelemetry/api` dependency (SDK is already available via env vars)
- **New:** `modules/agent-factory/agent/src/components/__tests__/otelStreamer.test.ts`

### Story B: Redis live-event publisher (agent-worker)
- **Modify:** `modules/agent-factory/agent/src/components/otelStreamer.ts` (or separate `liveEventPublisher.ts`) — Redis PUBLISH on each event
- **Modify:** `modules/agent-factory/agent-worker-image/entrypoint.py` — pass `REDIS_HOST` to agent env (or read from SSM)
- **Modify:** `modules/agent-factory/webhook-ingress/infra/scaledjob.tf` — add `REDIS_HOST`, `REDIS_AUTH_TOKEN` env vars (from existing SSM params)

### Story C: Gateway SSE endpoint
- **New:** `modules/gateway/src/activity/stream.py` — SSE endpoint with Redis subscribe + relay
- **Modify:** `modules/gateway/src/activity/routes.py` — mount the stream router
- **Modify:** `modules/gateway/src/shared/config.py` — add `activity_stream_enabled` flag
- **New:** `modules/gateway/tests/unit/test_activity_stream.py`

### Story D: Run activity summary (write path)
- **Modify:** Worker (via `OTelStreamer.destroy()`) — publish `run_complete` event with full summary
- **Modify:** Gateway SSE handler or a Lambda — on `run_complete` event, UpdateItem on DDB with `run_activity_summary`
- **Modify:** `modules/gateway/src/activity/service.py` — map `run_activity_summary` in `_map_item`
- **Modify:** `modules/gateway/src/activity/schemas.py` — add `run_activity_summary` field to `InvocationItem`

### Story E: Activity Detail Page (frontend)
- **New:** `modules/gateway/frontend/src/pages/ActivityDetail.tsx` — full page (route: `/activity/:invocationId`)
- **New:** `modules/gateway/frontend/src/hooks/useRunStream.ts` — `EventSource` hook
- **Modify:** `modules/gateway/frontend/src/App.tsx` — add route
- **Modify:** `modules/gateway/frontend/src/pages/AgentActivity.tsx` — row click navigates to detail page
- **New:** `modules/gateway/frontend/src/__tests__/pages/ActivityDetail.test.tsx`

### Story F: Infra wiring
- **Modify:** `modules/agent-factory/webhook-ingress/infra/scaledjob.tf` — Redis env vars
- **Modify:** `modules/agent-factory/webhook-ingress/infra/scaledjob-iam.tf` — (no IAM change needed; Redis is network-auth)
- **Modify:** `platform/infra/main.tf` — set `enable_xray_tracing = true` (unlocks gateway X-Ray permissions)

---

## 9. Child Story Decomposition (dependency-ordered)

```
#A  OTelStreamer: custom spans in agent-worker         (no deps)
#B  Redis live-event publisher in agent-worker         (depends on #A)
#C  Gateway SSE endpoint (Redis subscribe + relay)     (depends on #B)
#D  Run activity summary (write on completion)         (depends on #B, #C)
#E  Activity Detail Page (frontend)                    (depends on #C, #D)
#F  Infra: Redis env vars for worker + enable_xray     (no deps; can run first)
```

**Recommended execution order:** F → A → B → C → D → E

| Story | Est. size | Module(s) touched | Deploy path |
|-------|-----------|-------------------|-------------|
| F | S | `webhook-ingress/infra`, `platform/infra` | `agent-factory-infra-apply.yml` (manual) |
| A | M | `agent-factory/agent` | `agent-worker-image.yml` (auto on path change) |
| B | M | `agent-factory/agent`, `agent-worker-image` | `agent-worker-image.yml` |
| C | M | `gateway/src/activity` | `gateway-deploy.yml` (auto) |
| D | S | `gateway/src/activity`, `agent-factory/agent` | Both workflows |
| E | L | `gateway/frontend` | `gateway-deploy.yml` (auto) |

---

## 10. Non-Goals (explicit exclusions)

- **Prompt/tool-output content in spans** — deferred (#1630 data-governance decision). Only structural metadata flows.
- **Alerting on agent telemetry** — separate concern (ops-centre EPIC #872).
- **Real-time cost alerting / budget enforcement** — gateway budget Lambda is the existing mechanism.
- **X-Ray custom UI** — we render the timeline from `run_activity_summary` in our own UI; X-Ray Console is for deep-dive only.
- **Multi-trace correlation (chain-level trace grouping)** — X-Ray `session.id` already groups by `correlation_id`; no additional work needed.
- **WebSocket for live feed** — SSE is sufficient for unidirectional push; WebSocket adds complexity without benefit here.
- **Sampling** — not needed at current scale (~500 traces/day). Revisit if volume exceeds 10k/day.

---

## 11. Migration / Rollout Strategy

1. **Story F (infra)** can be applied immediately with no observable effect (just adds env vars the worker doesn't consume until Story B deploys).
2. **Story A (spans)** is additive-only — new spans flow to X-Ray alongside the existing SDK spans. No behavioral change.
3. **Story B (Redis pub)** is fire-and-forget — if no subscriber exists, events are silently dropped. Safe to deploy before Story C.
4. **Story C (SSE endpoint)** is gated by `activity_stream_enabled` config flag (default false). Enable after validation.
5. **Story D (summary write)** enriches existing DDB items — additive attribute, no schema migration. `_map_item` gracefully handles missing field (returns `None`).
6. **Story E (frontend)** is a new route — no change to existing Activity list page. Progressive enhancement.

**Rollback:** Each story is independently revertible:
- A: Remove `OTelStreamer` instantiation → spans stop. Worker continues normally.
- B: Remove Redis PUBLISH calls → no live events. Runs unaffected.
- C: Remove SSE endpoint or disable flag → 404 on stream requests.
- D: Stop writing `run_activity_summary` → detail page shows metadata-only (graceful).
- E: Remove route → 404 on detail page; list page still works.

---

## 12. Validation (per-story smoke tests)

| Story | Smoke test |
|-------|------------|
| F | `kubectl exec -n adp-agents <worker-pod> -- env \| grep REDIS_HOST` returns the ElastiCache endpoint |
| A | Trigger a run → X-Ray trace shows `agent-invocation` root span with child `turn.N` and grandchild `tool.*` spans, annotated with `adp.invocation_id` |
| B | `redis-cli SUBSCRIBE "run:{invocation_id}"` → see JSON events flow in real time during a run |
| C | `curl -H "Authorization: Bearer <token>" https://<gateway>/api/gateway/me/agent-invocations/{id}/stream` → SSE events appear for an in-progress run |
| D | After run completes, `GET /me/agent-invocations/{id}` response includes `run_activity_summary` with turns array |
| E | Navigate to `/activity/{invocation_id}` → see live turn feed while running, completed timeline after |

---

## 13. Cost Estimate

| Resource | Monthly cost (dev) |
|----------|--------------------|
| X-Ray traces (500/day × 5 spans) | ~$0.50 (within free tier) |
| Redis pub/sub messages | $0 (included in existing ElastiCache) |
| SSE connections | $0 (served by existing gateway pods) |
| DDB additional attribute writes | ~$0.01 (one UpdateItem per run) |
| **Total incremental** | **< $1/month in dev** |

At production scale (10x): still < $10/month. The architecture is inherently bounded by agent run volume, which is controlled by our existing rate-limiting and KEDA autoscaling.
