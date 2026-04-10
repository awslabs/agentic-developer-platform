# AWS Architecture — Dual Entry Point (CloudFront + API Gateway)

## Overview

Two entry points coexist in parallel. The existing CloudFront → ALB path continues to work unchanged. API Gateway is added as a new entry point for API traffic with centralized auth, streaming, and WAF.

---

## Architecture Diagram

```
                    ┌─────────────────────────────────────────────┐
                    │              Internet                        │
                    └──────────┬──────────────────┬───────────────┘
                               │                  │
                    ┌──────────▼──────────┐  ┌────▼──────────────────────┐
                    │   CloudFront        │  │   API Gateway (REST API)  │
                    │   (existing, no     │  │   (NEW)                   │
                    │    changes)         │  │                           │
                    │                     │  │   - Cognito authorizer    │
                    │   Routes:           │  │   - WAF                   │
                    │   /* → ALB (public) │  │   - Throttling/usage plans│
                    │                     │  │   - Response streaming    │
                    │   Used by:          │  │                           │
                    │   - Admin UI (SPA)  │  │   Routes:                 │
                    │   - Claude Code     │  │   /v1/*     → ALB (BG)    │
                    │     (Bedrock proxy) │  │   /bedrock/*→ ALB (BG)    │
                    │   - Any existing    │  │   /auth/*   → ALB (BG)    │
                    │     client          │  │   /admin/*  → ALB (BG)    │
                    │                     │  │   /mcp/*    → ALB (MCP)   │
                    └──────────┬──────────┘  │   /admin/mcp/* → ALB(MCP) │
                               │             │                           │
                               │             │   Used by:                │
                               │             │   - MCP clients           │
                               │             │   - New API consumers     │
                               │             │   - Can migrate existing  │
                               │             │     clients here over time│
                               │             └────┬──────────────────────┘
                               │                  │
                               │                  │ VPC Link v2 (private)
                               │                  │
                    ┌──────────▼──────────────────▼───────────────┐
                    │          Internal ALB (private VPC)          │
                    │                                              │
                    │  Target Group: bedrock-gateway (port 8000)   │
                    │    Path rules: /v1/*, /bedrock/*, /admin/*,  │
                    │                /auth/*, /health              │
                    │                                              │
                    │  Target Group: mcp-router (port 8001)        │
                    │    Path rules: /mcp/*, /admin/mcp/*          │
                    │                                              │
                    └──────────┬──────────────────┬───────────────┘
                               │                  │
                    ┌──────────▼──────┐  ┌────────▼──────────────┐
                    │ BedrockGateway  │  │ MCP Router            │
                    │ pods (EKS)      │  │ pods (EKS)            │
                    │                 │  │                       │
                    │ - Bedrock proxy │  │ - MCP proxy           │
                    │ - Admin API     │  │ - Catalog/deployment  │
                    │ - Auth          │  │ - Tool groups         │
                    │ - Budgets       │  │ - Discovery           │
                    │ - Rate limiting │  │ - Health monitoring   │
                    │ - Usage logging │  │ - Usage logging       │
                    └─────────────────┘  └───────────────────────┘
                               │                  │
                    ┌──────────▼──────────────────▼───────────────┐
                    │              Shared Resources                │
                    │                                              │
                    │  PostgreSQL (RDS) — different tables per svc │
                    │  Cognito user pool — shared auth             │
                    │  AgentCore Identity — MCP credential vault   │
                    │  ECR — separate repos per service            │
                    │  CloudWatch — metrics and logs               │
                    └─────────────────────────────────────────────┘
```

---

## Two Entry Points — Side by Side

| Aspect | CloudFront (existing) | API Gateway (new) |
|---|---|---|
| Status | Already working, no changes | New, additive |
| Public URL | `https://gateway.company.com` | `https://<api-id>.execute-api.<region>.amazonaws.com/<stage>` (or custom domain) |
| TLS | CloudFront handles | API Gateway handles |
| Auth | BedrockGateway middleware (SigV4/OIDC) | Cognito authorizer (JWT validated before reaching pods) |
| WAF | Can attach to CloudFront | Can attach to API Gateway |
| Streaming | CloudFront passes through SSE | API Gateway REST API with `responseTransferMode: STREAM` |
| Rate limiting | BedrockGateway app (token bucket) | API Gateway usage plans + app-level |
| MCP traffic | Not supported (no /mcp/* routes) | Supported (/mcp/* routes to MCP Router) |
| Used by | Existing Claude Code users, Admin UI | MCP clients, new API consumers |

---

## Migration Path

The two paths can coexist indefinitely. Over time, you can optionally migrate existing clients to API Gateway:

```
Phase 1 (now):
  CloudFront → ALB → BG pods     (existing clients, unchanged)
  API Gateway → ALB → MCP Router  (MCP clients only)
  API Gateway → ALB → BG pods     (new Bedrock API consumers)

Phase 2 (optional, future):
  API Gateway → ALB → BG pods     (all API traffic)
  API Gateway → ALB → MCP Router  (all MCP traffic)
  CloudFront → S3                  (Admin UI SPA only)
```

No rush to migrate. Both paths work. Claude Code users keep using `ANTHROPIC_BEDROCK_BASE_URL=https://gateway.company.com` through CloudFront. New MCP clients use the API Gateway URL.

---

## API Gateway Configuration Details

### REST API with Response Streaming

```yaml
# OpenAPI spec for API Gateway
x-amazon-apigateway-integration:
  type: HTTP_PROXY
  httpMethod: ANY
  uri: "http://internal-alb.vpc.local/{proxy}"
  connectionType: VPC_LINK
  connectionId: "<vpc-link-v2-id>"
  integrationTarget: "<alb-arn>"
  responseTransferMode: STREAM    # Enable streaming for SSE/MCP
  timeoutInMillis: 900000         # 15 minutes max
```

### VPC Link v2 → Private ALB

```
API Gateway REST API
    |
    | VPC Link v2 (one link, multiple ALB targets)
    |   - Subnets: same as ALB (multi-AZ)
    |   - Security group: allows traffic from API Gateway
    |
    v
Internal ALB (private, not internet-facing)
    - Listener: port 443 (HTTPS) or port 80 (HTTP, since traffic is already TLS-terminated)
    - Target groups: bedrock-gateway, mcp-router
```

### Cognito Authorizer

```
API Gateway Cognito Authorizer:
  - User Pool: same Cognito pool used by BedrockGateway
  - Token source: Authorization header
  - Validates JWT signature, expiry, audience
  - Passes claims to backend via headers
```

### Streaming Routes vs Buffered Routes

| Route Pattern | Transfer Mode | Why |
|---|---|---|
| `POST /v1/chat/completions` | STREAM | SSE streaming for chat completions |
| `POST /bedrock/invoke-with-response-stream` | STREAM | Bedrock streaming pass-through |
| `POST /v1/messages` | STREAM | Anthropic Messages streaming |
| `POST /mcp/*/mcp` | STREAM | MCP streamable HTTP transport |
| `GET /admin/*` | BUFFERED | Admin API (no streaming needed) |
| `GET /admin/mcp/*` | BUFFERED | MCP Admin API (no streaming needed) |
| `POST /auth/*` | BUFFERED | Auth endpoints (no streaming) |

---

## Infrastructure (Terraform)

### New Resources for API Gateway

```
infra/modules/apigateway/
  main.tf          # REST API, stages, deployment
  authorizer.tf    # Cognito authorizer
  vpc_link.tf      # VPC Link v2 to private ALB
  routes.tf        # Resource/method definitions with path routing
  streaming.tf     # Response streaming configuration per route
  waf.tf           # WAF association (optional)
  throttling.tf    # Usage plans and API keys
  variables.tf
  outputs.tf
```

### ALB Changes

The existing ALB changes from **internet-facing** to **internal** (private). CloudFront and API Gateway both reach it through their respective mechanisms:
- CloudFront → origin pointing to ALB DNS (works with internal ALB if CloudFront is in same region, or via VPC origin)
- API Gateway → VPC Link v2 → ALB (private integration)

**Important**: If CloudFront currently points to a public ALB, the migration to internal ALB needs careful planning to avoid downtime. Options:
1. Create a new internal ALB alongside the existing public one, migrate traffic gradually
2. Use CloudFront VPC origins (if available) to reach internal ALB
3. Keep ALB public but add security groups that only allow traffic from CloudFront and API Gateway

Option 3 is the safest for zero-disruption: ALB stays public, but security groups restrict access to CloudFront IP ranges + API Gateway VPC Link security group. No client-facing changes needed.

---

## References

- [API Gateway Response Streaming](https://aws.amazon.com/blogs/compute/building-responsive-apis-with-amazon-api-gateway-response-streaming/) — REST API streaming with SSE support
- [API Gateway Private Integration with ALB](https://aws.amazon.com/blogs/compute/build-scalable-rest-apis-using-amazon-api-gateway-private-integration-with-application-load-balancer/) — VPC Link v2 direct to ALB
- [API Gateway REST API Developer Guide](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-rest-api.html)
- [API Gateway Cognito Authorizer](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-integrate-with-cognito.html)
