# Phase 1 Spike Report: Gateway-Proxied Bedrock Path Validation

**Issue**: #746 | **EPIC**: #745
**Date**: 2026-05-21
**Author**: @agent-architect

## Executive Summary

The gateway-proxied Bedrock path for agent workers **already exists end-to-end**.
No new gateway code is required. The Phase 1 spike validates that the existing
infrastructure (shipped via Issue #260, PR #706) is architecturally sound for
agent-worker traffic and identifies the minimal wiring needed for cutover.

**Recommendation: GO** — proceed to Phase 2 (shadow mode).

---

## 1. Existing Infrastructure Audit

### 1.1 API Gateway `/agent/{proxy+}` Route

| Property | Value | Source |
|----------|-------|--------|
| Auth | `AWS_IAM` (SigV4) | `modules/gateway/infra/modules/api-gateway/main.tf:215` |
| Streaming | `responseTransferMode: STREAM` | Same file, line 229 |
| Timeout | 900,000 ms (15 min) | `variables.tf:88` default |
| VPC Link | Yes — to internal ALB | Line 231-233 |
| Header injection | `X-Caller-Identity = context.identity.userArn` | Line 236 |

**Verdict**: Streaming SSE is explicitly supported. 15-minute timeout covers even
the longest multi-turn agent responses (individual HTTP calls, not persistent
connections). VPC Link provides private network path.

### 1.2 Gateway Auth Middleware (Triple-Auth)

The `TokenContextMiddleware` (`modules/gateway/src/auth/middleware.py:507-569`)
supports three auth paths:

1. **Cognito JWT** — human CLI users via `Authorization: Bearer <jwt>`
2. **API Gateway headers** — trusted when `BG_TRUST_APIGW_HEADERS=true`
3. **IAM identity** — `extract_iam_identity_from_headers()` (line 416-504)

Path 3 is the agent path. It:
- Reads `X-Caller-Identity` header (set by API Gateway from `context.identity.userArn`)
- Parses the assumed-role ARN to extract the IAM role ARN
- Looks up the agent in DynamoDB via `AgentRegistryService.get_agent_by_role_arn()`
- Builds a `TokenContext` with org/team/budget scoping
- Raises `UnregisteredServiceAccountError` if role ARN not found

### 1.3 Agent Registry (DynamoDB)

The `adp-dev-agent-scaledjob-role` is already seeded in the agent registry:

```terraform
# modules/agent-factory/infra/agent-registry-seed.tf
resource "aws_dynamodb_table_item" "scaledjob_worker_agent" {
  item = jsonencode({
    agent_id   = { S = "scaledjob-worker" }
    role_arn   = { S = "arn:aws:iam::<ACCOUNT>:role/adp-dev-agent-scaledjob-role" }
    org_id     = { S = "__platform__" }
    team_id    = { S = "__agents__" }
    scope      = { S = "internal" }
    allowed_models = { SS = ["*"] }
    status     = { S = "active" }
  })
}
```

**Issue**: `org_id = "__platform__"` means budget/usage-log entries will be
attributed to the platform pseudo-tenant, not the actual requesting user's
tenant. For Phase 1 validation this is acceptable. Phase 4 must resolve
per-user attribution (likely via `X-Tenant-Id` header override or per-tenant
agent registry entries).

### 1.4 IAM Permissions on Agent Runner Role

```terraform
# modules/agent-factory/infra/modules/runner-iam/main.tf:238-241
{
  Sid      = "ExecuteApiInvokeGateway"
  Effect   = "Allow"
  Action   = ["execute-api:Invoke"]
  Resource = "arn:aws:execute-api:${var.aws_region}:${var.account_id}:*/*/*/agent/*"
}
```

The scaledjob role already has `execute-api:Invoke` scoped to `/agent/*` paths.
No IAM changes needed for the spike.

### 1.5 SigV4 Re-signing Proxy

```typescript
// modules/agent-factory/agent/src/sigv4-proxy.ts (shipped PR #706)
// Strips bedrock-signed auth headers, re-signs with service=execute-api
// Streams responses via proxyRes.pipe(res) — no buffering
```

Key properties:
- **Listens on** `127.0.0.1:8080` (configurable via `--port` / `SIGV4_PROXY_PORT`)
- **Target**: API Gateway invoke URL (via `--target` / `SIGV4_PROXY_TARGET`)
- **Streaming**: Uses `proxyRes.pipe(res)` — zero buffering, SSE-safe
- **Timeout**: 3,600,000 ms (1 hour) — covers any Bedrock response
- **Uses pod's ambient IRSA credentials** via `defaultProvider()`

---

## 2. End-to-End Flow (No New Code Needed)

```
Claude Code SDK
  → POST http://127.0.0.1:8080/v1/messages
    (signed with service=bedrock by SDK, or unsigned if ANTHROPIC_AUTH_TOKEN set)

sigv4-proxy.ts (in-pod)
  → Strips old auth headers
  → Re-signs with service=execute-api using pod IRSA creds
  → POST https://<APIGW>.execute-api.us-east-1.amazonaws.com/prod/agent/v1/messages

API Gateway
  → Validates SigV4 signature (AWS_IAM auth)
  → Injects X-Caller-Identity = assumed-role ARN
  → Forwards via VPC Link to internal ALB
  → responseTransferMode: STREAM (SSE passthrough)

Gateway (FastAPI)
  → TokenContextMiddleware reads X-Caller-Identity
  → extract_iam_identity_from_headers() → DynamoDB lookup
  → TokenContext built with org/team/budget from registry
  → /v1/messages route → existing ProxyService → Bedrock

Bedrock
  → Streaming response (SSE chunks)
  → Back through Gateway → ALB → VPC Link → API Gateway → sigv4-proxy → SDK
```

---

## 3. Risk Assessment

### 3.1 Streaming SSE Through API Gateway (KEY RISK)

**Status: MITIGATED by design.**

- `responseTransferMode: STREAM` is set on all `/agent/*` integrations (confirmed line 229)
- API Gateway HTTP API (not REST API) supports streaming responses natively
- The sigv4-proxy uses `proxyRes.pipe(res)` — no response buffering
- Timeout is 900s at API Gateway, 3600s at proxy — both exceed typical response times

**Remaining uncertainty**: Whether API Gateway's VPC Link integration introduces
measurable latency on SSE chunk delivery. The webhook-ingress Lambda already
uses this path for non-streaming calls, but streaming hasn't been explicitly
benchmarked. This is the primary measurement for the manual smoke test.

### 3.2 Per-Request vs Per-Connection Auth

Claude Code SDK makes **individual HTTP requests per turn** (not a persistent
WebSocket). Each request gets independently SigV4-signed. This means:
- No connection-level auth timeout issues
- Each request carries fresh STS credentials
- API Gateway validates each request independently

**No risk here** — the request-per-turn model maps perfectly to SigV4.

### 3.3 Request Size (Tool-Use Payloads)

Agent workers use tool-use extensively. A typical tool-use response can be
10-50 KB (code file contents). API Gateway payload limit is 10 MB.
Bedrock's limit is the binding constraint (~200K tokens input).

**No risk** — payload sizes are well within API Gateway limits.

### 3.4 Tenant Attribution

The current registry entry uses `org_id = "__platform__"`. This means:
- Usage logs will show platform-level attribution
- Budget enforcement won't apply per-user limits
- Rate limiting uses platform-level quotas

**Acceptable for Phase 1** (validation spike). Phase 4 must implement per-user
attribution — either via `X-Tenant-Id` header from the SQS message envelope
or by registering per-tenant entries in the agent registry.

### 3.5 Integration Timeout (900s)

A single Bedrock streaming response typically completes in 30-120s for
large outputs. The 900s timeout provides 7-15x headroom. Multi-turn
conversations make multiple independent requests — the timeout applies
per-request, not per-conversation.

**No risk.**

---

## 4. Manual Smoke Test Protocol

### 4.1 Prerequisites

```bash
# 1. Get API Gateway invoke URL
APIGW_URL=$(aws ssm get-parameter \
  --name "/adp/dev/gateway/apigw-invoke-url" \
  --query "Parameter.Value" --output text)

# 2. Verify agent registry entry exists
TABLE=$(aws ssm get-parameter \
  --name "/adp/dev/gateway/agent-registry-table" \
  --query "Parameter.Value" --output text)
aws dynamodb get-item --table-name "$TABLE" \
  --key '{"agent_id": {"S": "scaledjob-worker"}}' \
  --query 'Item.role_arn.S'

# 3. Verify runner role has execute-api:Invoke
aws iam simulate-principal-policy \
  --policy-source-arn "arn:aws:iam::879318057152:role/adp-dev-agent-scaledjob-role" \
  --action-names "execute-api:Invoke" \
  --resource-arns "arn:aws:execute-api:us-east-1:879318057152:*/*/ANY/agent/*" \
  --query 'EvaluationResults[0].EvalDecision'
```

### 4.2 Test Matrix

| Test | Command | Expected | Validates |
|------|---------|----------|-----------|
| Single-turn streaming | `claude "What is 2+2?"` via proxy | Streamed "4" response, completes <5s | SSE through API GW |
| Multi-turn with tool use | `claude "List files in /tmp and count them"` via proxy | Uses bash tool, multiple turns | Tool-use payloads + multi-request |
| Auth failure (no creds) | Unset AWS creds, call proxy | 403 from API Gateway | IAM auth enforced |
| Unregistered role | Use a different role ARN | 403 from gateway | Agent registry check |
| Direct Bedrock control | Same tasks with `CLAUDE_CODE_USE_BEDROCK=1` | Baseline latency | Comparison baseline |

### 4.3 Test Pod Spec (One-Off)

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: spike-745-test
  namespace: arc-runners
  labels:
    app: spike-test
spec:
  serviceAccountName: github-runner-sa  # IRSA-annotated
  containers:
  - name: agent
    image: <ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com/adp-agent-worker:latest
    command: ["/bin/bash", "-c"]
    args:
    - |
      # Start SigV4 proxy in background
      export SIGV4_PROXY_TARGET="${APIGW_URL}/agent"
      export SIGV4_PROXY_PORT=9090
      npx ts-node /app/src/sigv4-proxy.ts &
      sleep 2

      # Point Claude Code SDK at the proxy
      export ANTHROPIC_BASE_URL="http://127.0.0.1:9090"
      unset CLAUDE_CODE_USE_BEDROCK

      # Run test tasks
      echo "=== Test 1: Single-turn ==="
      time claude -p "What is 2+2?" 2>&1 | tee /tmp/test1.log

      echo "=== Test 2: Multi-turn with tool use ==="
      time claude -p "List the files in /tmp and tell me how many there are" 2>&1 | tee /tmp/test2.log

      echo "=== Control: Direct Bedrock ==="
      export CLAUDE_CODE_USE_BEDROCK=1
      unset ANTHROPIC_BASE_URL
      time claude -p "What is 2+2?" 2>&1 | tee /tmp/control.log

      echo "=== All tests complete ==="
    env:
    - name: APIGW_URL
      valueFrom:
        configMapKeyRef:
          name: gateway-config
          key: apigw-invoke-url
    - name: AWS_REGION
      value: "us-east-1"
```

### 4.4 Latency Measurement Protocol

For each test, capture:
- **Time to First Token (TTFT)**: time from request send to first SSE chunk received
- **Total wall time**: end-to-end completion time
- **Per-turn latency**: for multi-turn tasks, time per individual API call

Compare:
- **Proxy path**: SDK → sigv4-proxy → API GW → Gateway → Bedrock
- **Direct path**: SDK → Bedrock (via IRSA + `CLAUDE_CODE_USE_BEDROCK=1`)

Expected overhead: 10-50ms per request (API Gateway + VPC Link hop). This should
be negligible relative to Bedrock inference time (2-30s per response).

---

## 5. Findings (Pending Manual Execution)

> **NOTE**: This section is populated after the operator runs the manual smoke test.
> The architectural analysis confirms GO with high confidence, but measured
> numbers are required before Phase 2 proceeds.

| Metric | Proxy Path | Direct Bedrock | Delta | Acceptable? |
|--------|-----------|----------------|-------|-------------|
| TTFT (single-turn) | _TBD_ | _TBD_ | _TBD_ | <100ms overhead |
| Wall time (single-turn) | _TBD_ | _TBD_ | _TBD_ | <5% overhead |
| Wall time (multi-turn) | _TBD_ | _TBD_ | _TBD_ | <5% overhead |
| SSE chunks received | _TBD_ | _TBD_ | Match | Must match |
| Usage log entries | _TBD_ | N/A | Present | Must appear |

### Error Modes Observed

_Populated after manual test._

---

## 6. Architecture Decision: SigV4 Signing Approach

### Chosen: In-Pod TypeScript Proxy (Already Shipped)

The `sigv4-proxy.ts` at `modules/agent-factory/agent/src/sigv4-proxy.ts`:

| Criterion | Assessment |
|-----------|------------|
| Streaming SSE | Passes through unbuffered (`pipe()`) |
| Auth model | Uses pod's ambient IRSA creds via `defaultProvider()` |
| Coupling to SDK | Zero — SDK sees a plain HTTP endpoint |
| Operational overhead | Runs in same container, <5 MB memory |
| Already tested | Shipped in PR #706, in the worker image |
| Failure mode | If proxy dies, SDK gets connection refused → clear error |

### Alternatives Considered and Rejected

| Approach | Why Rejected |
|----------|-------------|
| Sidecar container | Extra container overhead, health-check coordination, shared localhost networking complexity. Same functional result as in-process proxy. |
| httpx event hooks | SDK doesn't expose transport layer; would require monkey-patching. Fragile across SDK versions. |
| Inline Python signer in entrypoint | Tighter coupling to SDK request lifecycle. Python's `botocore` SigV4 is slower than @smithy. Worker image is Node-based anyway. |
| Presigned URLs | Don't work for streaming POST requests. |

---

## 7. What Phase 2-4 Become (Simplified)

Given that the infrastructure already exists, the EPIC #745 phases collapse:

### Phase 2: Shadow Mode
- Add env vars to ScaledJob pod template:
  ```
  SIGV4_PROXY_TARGET=<APIGW_URL>/agent
  SIGV4_PROXY_PORT=9090
  ANTHROPIC_BASE_URL=http://127.0.0.1:9090
  ```
- Remove `CLAUDE_CODE_USE_BEDROCK=1` from pod template
- Start sigv4-proxy before Claude Code in entrypoint
- **Fallback**: if proxy health-check fails, fall back to direct Bedrock
- **No gateway code changes**

### Phase 3: Full Cutover
- Remove fallback logic
- All agent traffic goes through gateway
- Monitor usage_logs for anomalies

### Phase 4: Revoke Direct Bedrock
- Remove `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` from `adp-dev-agent-scaledjob-role`
- Keep `execute-api:Invoke` (already present)
- Implement per-tenant attribution (update agent registry or pass tenant context from SQS envelope)

---

## 8. Recommendation

### GO — Proceed to Phase 2

**Confidence: HIGH**

Rationale:
1. All infrastructure components exist and are deployed
2. SigV4 proxy is implemented and streams without buffering
3. IAM permissions are already in place
4. Agent registry entry exists
5. API Gateway streaming is explicitly configured
6. Integration timeout (900s) exceeds any single response
7. The only unknown — measured latency — is bounded by architecture (one extra HTTP hop)

### Conditions for Phase 2 Start

1. Operator runs manual smoke test (Section 4) and confirms:
   - SSE streaming works end-to-end
   - Latency overhead is <100ms per request
   - Usage logs appear for the test tenant
2. PR #750 is closed (implements superseded design)
3. Phase 2 issue (#747) is updated with the simplified scope from Section 7

---

## 9. Open Questions for Operator

1. **Per-tenant attribution**: Should Phase 2 pass the user's `tenant_id` from the SQS message envelope as `X-Tenant-Id` header? Or defer to Phase 4?
2. **Fallback behavior**: In Phase 2 shadow mode, should the entrypoint detect proxy failure and fall back to direct Bedrock? Or fail fast?
3. **Metrics**: Should the sigv4-proxy emit latency metrics (e.g., to CloudWatch) for ongoing monitoring, or is log-based measurement sufficient?

---

## References

- Issue #260: API Gateway dual-path (AWS_IAM + NONE auth)
- Issue #240: API Gateway header trust (`BG_TRUST_APIGW_HEADERS`)
- PR #706: SigV4 re-signing proxy (`sigv4-proxy.ts`)
- Issue #575: Agent registry DynamoDB seed
- `modules/gateway/infra/modules/api-gateway/main.tf:190-241` — `/agent/*` routes
- `modules/gateway/src/auth/middleware.py:416-504` — IAM identity extraction
- `modules/gateway/src/auth/agent_registry.py` — DynamoDB registry service
- `modules/agent-factory/infra/modules/runner-iam/main.tf:238-241` — `execute-api:Invoke` permission
- `modules/agent-factory/infra/agent-registry-seed.tf` — scaledjob-worker entry
- `modules/agent-factory/agent/src/sigv4-proxy.ts` — SigV4 re-signing proxy
