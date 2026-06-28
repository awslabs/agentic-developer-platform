# Design Note: External Agent Integration — Cross-Service Consumer Design

**Issue:** #2212 (umbrella EPIC)
**Child EPICs:** #2033 (trigger), #2115 (streaming)
**Author:** @agent-architect
**Date:** 2026-06-28
**Status:** Design-complete — Phase 2 child stories ready to file

---

## 1. Executive Summary

External consumers need to both **invoke** ADP agents and **stream their activity** through one coherent experience. This design defines the cross-service path connecting the trigger EPIC (#2033) and the streaming EPIC (#2115): one credential model, one invoke→subscribe flow, one identity model spanning both directions.

**The core insight:** the trigger call returns an `{invocation_id, correlation_id}` that the streaming side uses as the subscription handle, scoped to runs the caller owns.

---

## 2. Auth Model: Hybrid (API Key + OAuth2 Client-Credentials)

### Phase 2a (first ship): API Key

One static key per registered integration, stored hashed in DynamoDB:

| Layer | Mechanism |
|-------|-----------|
| Transport | Custom header: `X-ADP-API-Key: sk_live_<random>` |
| Server-side | SHA-256 hash → lookup table → integration_id → identity-index row |
| Scope | `scopes` attribute on identity-index row: `["invoke", "subscribe"]` |
| Rate limit | Per-integration, reusing existing `RateLimiter` with integration_id as key |
| Revocation | Admin sets `status = "revoked"` on identity-index row; key immediately rejected |

### Phase 2b (production upgrade): OAuth2 Client-Credentials

Standard token exchange:
```
POST /auth/integrations/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=<id>&client_secret=<secret>&scope=invoke+subscribe
```

Returns a short-lived JWT (15-min) with claims: `integration_id`, `tenant_id`, `allowed_personas`, `scopes`. Validated by the gateway using the same JWT middleware as Cognito tokens (different issuer, same validation path).

**Why hybrid?** API key is zero-infrastructure (DDB lookup only). OAuth2 adds a token endpoint + token storage + refresh flow (~3 extra stories). Ship API key first, upgrade integrations that need short-lived tokens later. Both map to the same identity-index row.

---

## 3. Identity Model: `identity_type = "external_integration"`

Extends the existing `adp-dev-identity-index` DynamoDB table:

```
PK: identity_type = "external_integration"
SK: identity_value = "<integration_id>"  (UUID, assigned at registration)

Attributes:
  tenant_id: str              — ADP tenant this integration belongs to
  org_id: str                 — org context (= tenant_id in most cases)
  display_name: str           — human label ("Acme CI Bot")
  api_key_hash: str           — SHA-256 of the issued API key
  allowed_personas: list[str] — personas this integration can spawn
  scopes: list[str]           — ["invoke", "subscribe"]
  rate_limit_per_window: int  — per-5-min cap (default: 20)
  rate_limit_per_hour: int    — hourly cap (default: 200)
  status: str                 — "active" | "suspended" | "revoked"
  created_at: str             — ISO 8601
  created_by: str             — user_id of the registering admin
```

### Separate key-lookup table: `adp-<env>-integration-keys`

```
PK: api_key_hash (SHA-256, hex-encoded)

Attributes:
  integration_id: str  — maps to identity-index SK
  tenant_id: str       — denormalized for fast auth
  status: str          — "active" | "revoked" (denormalized)
  created_at: str
```

**Why a separate table?** The identity-index PK is `(identity_type, identity_value)` — not the key hash. A lookup by key hash would require either a full-table scan (unacceptable) or a GSI on a credential-derived column (security concern). A dedicated table with PK=hash gives O(1) lookup with minimal blast radius.

### Why not reuse `"service_account"`?

| Property | service_account | external_integration |
|----------|----------------|---------------------|
| Provisioned by | Platform operators (Terraform/CLI) | Tenant admins (self-service UI) |
| Credential material in DDB | None (EventBridge delivers natively) | API key hash (user-facing) |
| Lifecycle | Static (lives as long as the rule) | Dynamic (create/suspend/revoke via UI) |
| Risk profile | Trusted internal (VPC-internal, no credential to steal) | External-facing (credential exposure = unauthorized spawn) |

Separate `identity_type` prevents accidental privilege escalation (a service_account lookup returning an external-facing credential, or vice versa).

---

## 4. External Trigger Endpoint

### Request

```http
POST /integrations/v1/invoke
X-ADP-API-Key: sk_live_abc123...
Content-Type: application/json

{
  "persona": "developer",
  "repo": "acme-corp/backend",
  "issue_number": 42,
  "reason": "Deploy pipeline failed — triage needed",
  "target": {
    "sha": "abc123",
    "branch": "main"
  }
}
```

### Server Flow

1. Hash API key → look up in `integration-keys` table
2. Resolve full integration row from identity-index
3. Verify `status == "active"`, `"invoke" in scopes`
4. Rate-limit (integration_id as key, existing `RateLimiter`)
5. Validate `persona in allowed_personas`
6. Build ROOT lineage:
   ```python
   correlation_ctx = {
       "correlation_id": uuid4(),
       "root_human_id": integration_id,
       "is_human_rooted": False,
       "chain_depth": 0,
       ...
   }
   ```
7. Call `spawn_persona()` (single enforcement point, reused)
8. Return response

### Response

```json
{
  "status": "accepted",
  "invocation_id": "a1b2c3d4-...",
  "correlation_id": "e5f6g7h8-...",
  "subscribe_url": "/integrations/v1/runs/a1b2c3d4-.../stream"
}
```

### Where it lives

New handler module: `modules/agent-factory/webhook-ingress/lambda/external/handler.py`

New route on webhook-ingress API Gateway: `POST /integrations/v1/invoke`, dispatched to the Lambda. Follows the same pattern as `eventbridge/handler.py` — resolves identity, enforces guards, calls `spawn_persona()`.

---

## 5. External Subscribe Endpoint

### Request

```http
GET /integrations/v1/runs/{invocation_id}/stream
X-ADP-API-Key: sk_live_abc123...
Accept: text/event-stream
Last-Event-ID: 3  (optional, for reconnect)
```

### Transport

API Gateway REST (`responseTransferMode=STREAM`) → VPC Link → ALB → Gateway pod SSE endpoint.

The gateway pod:
1. Validates the API key (same lookup as trigger)
2. Verifies `"subscribe" in scopes`
3. Verifies ownership: DDB `webhook-events` item has `root_human_id == integration_id`
4. Subscribes to Redis channel `run:{invocation_id}`
5. Relays events as SSE frames
6. Sends `:keepalive\n\n` every 30 seconds (prevents API GW idle timeout)
7. On `run.completed` event: sends it, then closes the stream

### Reconnect / Replay

| Scenario | Behavior |
|----------|----------|
| Client sends `Last-Event-ID` | Gateway replays from ring buffer (last 50 events per active run) |
| Run already completed | Gateway returns single `run.completed` event from `run_activity_summary` DDB attribute, then closes |
| Run not yet started | Gateway waits on Redis subscribe; first event will be `run.started` |
| Connection exceeds 15 min | Client reconnects; gateway re-subscribes and replays from ring buffer |

### Chain visibility

An integration that roots a chain sees all descendant runs in that correlation_id. Subscribe with `invocation_id` shows that specific run; subscribe with `correlation_id` (future enhancement) shows all runs in the chain.

---

## 6. End-to-End Sequence

```
1. REGISTER (one-time, tenant admin in ADP UI)
   Admin → POST /admin/integrations
        → {integration_id, api_key} (key shown once)

2. INVOKE (per-request, external app)
   App → POST /integrations/v1/invoke
       → 202 {invocation_id, correlation_id, subscribe_url}

3. SUBSCRIBE (immediately after invoke)
   App → GET /integrations/v1/runs/{invocation_id}/stream
       → SSE: run.started
       → SSE: run.turn {turn:1, ...}
       → SSE: run.tool {tool:"Bash", ...}
       → SSE: run.turn {turn:2, ...}
       → ...
       → SSE: run.completed {summary}
       → (connection closes)

4. POLL (optional, for status without streaming)
   App → GET /integrations/v1/runs/{invocation_id}
       → {status, summary, xray_trace_id}
```

---

## 7. Error Contract (consistent across all integration endpoints)

```json
// 401 Unauthorized
{"error": "unauthorized", "message": "Invalid or expired API key"}

// 403 Forbidden
{"error": "forbidden", "message": "Scope 'invoke' not granted"}
{"error": "persona_not_allowed", "allowed": ["developer", "operations"]}
{"error": "integration_suspended", "message": "Integration is suspended by admin"}

// 404 Not Found
{"error": "not_found", "message": "Invocation not found or not owned"}

// 429 Rate Limited
{"error": "rate_limited", "retry_after": 180, "limit": 20, "window": "2026-06-28T12:00"}

// 500 Internal
{"error": "internal", "message": "Failed to enqueue"}
```

---

## 8. Phase-2 Child Stories (dependency-ordered)

```
Phase 1 (already designed, internal ADP UI):
  F → A → B → C → D → E

Phase 2 (external integration — this design):
  I → G → H

Dependencies:
  I (registration) has NO Phase-1 dependency — can start immediately
  G (trigger) depends on: I + Phase 1 F (Redis env in ScaledJob)
  H (subscribe) depends on: I + Phase 1 B (worker publishes) + Phase 1 C (SSE relay)
```

### Story I: Integration Registration + Credential Issuance
- **Parent:** #2212 (umbrella — cross-cutting)
- **What:** Admin CRUD endpoints + DDB table + key generation
- **Size:** M
- **Module:** `modules/gateway/src/admin/`, `modules/agent-factory/webhook-ingress/infra/`
- **Deploy:** `gateway-deploy.yml` + `agent-factory-infra-apply.yml`

### Story G: External Trigger Endpoint
- **Parent:** #2033
- **What:** Lambda handler + API GW route + auth + spawn_persona call
- **Size:** M
- **Module:** `modules/agent-factory/webhook-ingress/lambda/external/`
- **Deploy:** `webhook-ingress-deploy.yml`

### Story H: External Subscribe (SSE + Integration Auth)
- **Parent:** #2115
- **What:** Gateway endpoint + API-key auth + Redis subscribe + keepalive + ownership check
- **Size:** M
- **Module:** `modules/gateway/src/activity/`
- **Deploy:** `gateway-deploy.yml`

---

## 9. Security Considerations

| Surface | Threat | Mitigation |
|---------|--------|------------|
| API key in transit | Interception → unauthorized invoke/subscribe | TLS-only (API GW enforces HTTPS); key prefix `sk_live_` makes leaks grep-able |
| API key at rest | DB compromise → all keys exposed | Stored as SHA-256 hash; raw key shown once at creation, never retrievable |
| Brute-force key guessing | Attacker tries random keys | 256-bit key space + rate limit on auth failures (429 after 5 bad keys/min per source IP) |
| Cross-tenant subscribe | Integration A sees Integration B's runs | Ownership check: `root_human_id == integration_id` on the DDB item |
| Forge root identity | Request body claims arbitrary root | Root identity is ALWAYS server-resolved from the credential, never from the request body |
| Unbounded SSE connections | Resource exhaustion | Per-integration connection cap (5 concurrent) + per-pod cap (100 total) |
| Long-running stream abuse | Single stream holds resources indefinitely | API GW 15-min max + server-side timeout after run completion |

---

## 10. Cost Estimate (incremental for Phase 2)

| Resource | Monthly cost (dev) |
|----------|--------------------|
| DDB integration-keys table (PAY_PER_REQUEST) | ~$0.01 (< 1000 lookups/day) |
| DDB identity-index GSI | $0 (GSI already exists; new rows are free) |
| Lambda additional invocations (trigger) | ~$0.05 (< 500/day) |
| SSE connections on gateway pods | $0 (served by existing pods) |
| API GW additional requests | ~$0.10 (< 5000/day within free tier) |
| **Total incremental** | **< $0.50/month in dev** |

---

## 11. Non-Goals

- OAuth2 client-credentials grant (Phase 2b — deferred to after API key proves the pattern)
- Webhook callback mode (push on completion — noted as future enhancement)
- Cross-tenant integration sharing (an integration belongs to exactly one tenant)
- Admin-created integrations on behalf of other tenants (only platform admins via direct DDB)
- Integration-scoped billing/quota (uses the tenant's existing quota)
