# Gateway Endpoint Audit — 2026-05-25

## Summary

| Metric | Count |
|--------|-------|
| Total endpoints cataloged | 147 |
| Routed via CloudFront `/api/*` (reachable externally) | ~95 |
| Blocked by CloudFront (SPA fallback, not exposed externally) | ~52 |
| Tested unauthenticated via CloudFront | 95 |
| Public (no auth required) | 6 |
| Protected (returns 401 without token) | ~89 |
| Tested authenticated (OAuth) | 0 (see Constraints) |
| Tested authenticated (IAM SigV4) | 0 (see Constraints) |
| Tools/tool_choice preserving (code analysis) | 2 (`/model/{id}/invoke`, `/bedrock/invoke`) |
| Tools/tool_choice **DROPPING** (code analysis) | 2 (`/v1/messages`, `/v1/chat/completions`) |

### Critical Finding: tools/tool_choice dropped on /v1/messages path

The `anthropic_to_bedrock()` translator (format_translator.py:215-263) does NOT copy `request.tools` or `request.tool_choice` to the `BedrockInvokeRequest`, even though both the input schema (`AnthropicMessagesRequest.tools`, line 236) and output schema (`BedrockInvokeRequest.tools`, line 340) support them. This confirms issue #790.

**Pass-through paths** (`/model/{id}/invoke`, `/bedrock/invoke`) are NOT affected because they use `BedrockInvokeRequest(**body)` directly (service.py:284), preserving all fields including tools.

## Constraints

This audit was performed from an agent pod (`adp-dev-agent-scaledjob-role`) with limited permissions:
- **No Cognito JWT available** — cannot test authenticated endpoint behavior
- **No kubectl access to adp-gateway namespace** — cannot test via cluster-internal paths
- **No API Gateway describe permissions** — cannot verify REST API stage mappings for IAM/SigV4 path
- **No SSM access** — cannot read parameter store values

**What IS tested**: every endpoint reachable via CloudFront, unauthenticated, to determine routing and auth requirements. Tools preservation determined by code analysis.

## Auth Modes

### Mode A — Cognito OAuth (human / dashboard / direct API)

- **Flow**: User authenticates via Cognito Hosted UI (PKCE) or client_credentials grant → receives JWT → passes as `Authorization: Bearer <token>` or `X-Api-Key: <token>`
- **Exposed via**: CloudFront (`dp7n42m5j4pl6.cloudfront.net`) → VPC Origin → internal ALB → gateway pod
- **CloudFront strips**: `/api` prefix (client sends `/api/v1/messages`, gateway sees `/v1/messages`)
- **How to obtain token**: `modules/gateway/cli/bg-cognito-auth.sh login --gateway-url https://dp7n42m5j4pl6.cloudfront.net/api`
- **Token validation**: `src/auth/middleware.py:validate_cognito_jwt()` → verifies signature against Cognito JWKS

### Mode B — IAM SigV4 (agents via API Gateway)

- **Flow**: Agent SDK → SigV4 signing → API Gateway REST API `bedrockgw-dev-api` (id `59o2rakc50`) → `/agent/{proxy+}` → VPC Link → internal ALB → gateway pod
- **Exposed via**: API Gateway REST API endpoint
- **Auth mechanism**: API Gateway IAM authorizer validates SigV4 signature; gateway trusts `X-Auth-Source` header from API GW when `BG_TRUST_APIGW_HEADERS=true` (see `proxy/routes.py:152-158`)
- **Agent-side proxy**: `modules/agent-factory/agent/src/sigv4-proxy.ts` runs at `127.0.0.1:9090`, adds SigV4 to outgoing requests
- **Token context extraction**: `src/auth/middleware.py:extract_api_gateway_context()` reads pre-validated headers

### CloudFront Routing Behavior

CloudFront distribution `dp7n42m5j4pl6` has cache behaviors that route to different origins:

| Path pattern | Origin | Server header | Notes |
|---|---|---|---|
| `/api/health`, `/api/ready`, `/api/v1/*`, `/api/auth/(exchange\|me\|logout\|revoke\|service-accounts)`, `/api/admin/*`, `/api/bedrock/*`, `/api/model/*`, `/api/usage/*`, `/api/budgets/*`, `/api/ratelimits/*` | VPC Origin (ALB → uvicorn) | `server: uvicorn` | Gateway API endpoints |
| `/api/internal/*`, `/api/auth/credentials/*`, `/api/auth/identities*`, `/api/auth/link/*`, `/api/auth/vault/*`, `/api/api/admin/*`, `/api/access/*` | S3 (frontend bucket) | `server: AmazonS3` | Falls through to SPA — these paths are NOT exposed via CloudFront |
| `/*` (everything else) | S3 (frontend bucket) | `server: AmazonS3` | React SPA with fallback to index.html |

**Security implication**: Internal endpoints (`/internal/v1/*`) and vault credential endpoints (`/auth/credentials/*`, `/auth/identities/*`) are NOT reachable via CloudFront. They are only accessible via:
1. The API Gateway `/agent/*` path (IAM SigV4 required)
2. Direct ALB access (currently internal-only ALB in private subnets)

## Endpoint Matrix

### Proxy Endpoints (src/proxy/routes.py)

| # | Method | Path | Line | Auth | CF Routed | Status (unauth) | Tools preserved | Notes |
|---|--------|------|------|------|-----------|-----------------|-----------------|-------|
| 1 | GET | `/health` | app.py:180 | none | yes | 200 | N/A | Root health check |
| 2 | GET | `/ready` | app.py:185 | none | yes | 200 | N/A | Readiness probe |
| 3 | GET | `/v1/health` | routes.py:671 | none | yes | 200 | N/A | Proxy health check |
| 4 | GET | `/v1/models` | routes.py:266 | Cognito JWT | yes | 401 | N/A | Lists available models |
| 5 | POST | `/v1/chat/completions` | routes.py:228 | Cognito JWT | yes | 401 | **NO** — goes through `openai_to_bedrock()` which does not copy tools | OpenAI-compatible |
| 6 | POST | `/v1/messages` | routes.py:289 | Cognito JWT | yes | 401 | **NO** — `anthropic_to_bedrock()` (format_translator.py:242-263) does not copy `request.tools` or `request.tool_choice` to `BedrockInvokeRequest` | Anthropic Messages — **confirms #790** |
| 7 | POST | `/v1/messages/count_tokens` | routes.py:383 | Cognito JWT | yes | 401 | N/A | Token estimation only |
| 8 | POST | `/bedrock/invoke` | routes.py:431 | Cognito JWT | yes | 401 | **YES** — body passed as-is to `proxy_service.invoke_model()` which does `BedrockInvokeRequest(**body)` | Bedrock pass-through |
| 9 | POST | `/bedrock/invoke-with-response-stream` | routes.py:469 | Cognito JWT | yes | 401 | **YES** — same pass-through path | Bedrock streaming |
| 10 | POST | `/model/{model_id}/invoke` | routes.py:521 | Cognito JWT | yes | 401 | **YES** — `proxy_service.invoke_model(model_id, body, ...)` preserves all body fields | SDK URL pattern |
| 11 | POST | `/model/{model_id}/invoke-with-response-stream` | routes.py:602 | Cognito JWT | yes | 401 | **YES** — same pass-through | SDK URL streaming |

### Auth Endpoints (src/auth/routes.py)

| # | Method | Path | Line | Auth | CF Routed | Status (unauth) | Notes |
|---|--------|------|------|------|-----------|-----------------|-------|
| 12 | POST | `/auth/exchange` | routes.py:46 | none (self-authenticating) | yes | 410 (Gone) | **DEPRECATED** — disabled by default (BG_ENABLE_LEGACY_AUTH_EXCHANGE=false) |
| 13 | GET | `/auth/me` | routes.py:142 | Cognito JWT | yes | 401 | Returns current user context |
| 14 | POST | `/auth/logout` | routes.py:167 | Cognito JWT | yes | 401 | Logout (client-side for Cognito) |
| 15 | POST | `/auth/revoke` | routes.py:191 | Cognito JWT | yes | 401 | Revoke token |
| 16 | POST | `/auth/service-accounts` | routes.py:221 | Cognito JWT + admin | yes | 401 | Create service account |
| 17 | GET | `/auth/service-accounts` | routes.py:263 | Cognito JWT | yes | 401 | List service accounts |
| 18 | GET | `/auth/service-accounts/{id}` | routes.py:290 | Cognito JWT | yes | 401 | Get service account |
| 19 | PUT | `/auth/service-accounts/{id}` | routes.py:314 | Cognito JWT + admin | yes | 401 | Update service account |
| 20 | DELETE | `/auth/service-accounts/{id}` | routes.py:348 | Cognito JWT + admin | yes | 401 | Delete service account |
| 21 | POST | `/auth/admin/cleanup-tokens` | routes.py:388 | Cognito JWT + admin | yes | 401 | Admin: clean expired tokens |
| 22 | POST | `/auth/admin/revoke-user-tokens/{user_id}` | routes.py:411 | Cognito JWT + admin | yes | 401 | Admin: revoke all user tokens |

### Vault Endpoints (src/auth/vault_routes.py) — NOT routed via CloudFront

| # | Method | Path | Line | Auth | CF Routed | Notes |
|---|--------|------|------|------|-----------|-------|
| 23 | GET | `/auth/credentials` | vault_routes.py:108 | Cognito JWT | **NO** (S3 fallback) | List user credentials |
| 24 | POST | `/auth/credentials` | vault_routes.py:131 | Cognito JWT | **NO** | Create credential |
| 25 | PATCH | `/auth/credentials/{id}` | vault_routes.py:160 | Cognito JWT | **NO** | Update credential metadata |
| 26 | DELETE | `/auth/credentials/{id}` | vault_routes.py:183 | Cognito JWT | **NO** | Delete credential |
| 27 | GET | `/auth/identities` | vault_routes.py:213 | Cognito JWT | **NO** | List linked identities |
| 28 | DELETE | `/auth/identities/{id}` | vault_routes.py:228 | Cognito JWT | **NO** | Unlink identity |
| 29 | POST | `/auth/identities/{provider}/link` | vault_routes.py:289 | Cognito JWT | **NO** | Issue magic-link for identity linking |
| 30 | GET | `/auth/link/magic` | vault_routes.py:364 | none (token in query) | **NO** | Magic-link landing page |
| 31 | POST | `/auth/link/magic` | vault_routes.py:440 | none (token in body) | **NO** | Confirm magic-link |

### AWS Connect Endpoints (src/auth/aws_connect_routes.py) — NOT routed via CloudFront

| # | Method | Path | Line | Auth | CF Routed | Notes |
|---|--------|------|------|------|-----------|-------|
| 32 | POST | `/auth/credentials/aws/connect` | aws_connect_routes.py:118 | Cognito JWT | **NO** | Initiate AWS account connection |
| 33 | POST | `/auth/credentials/aws/verify` | aws_connect_routes.py:208 | Cognito JWT | **NO** | Verify AWS role creation |

### Admin Endpoints (src/admin/routes.py)

| # | Method | Path | Line | Auth | CF Routed | Status (unauth) | Notes |
|---|--------|------|------|------|-----------|-----------------|-------|
| 34 | POST | `/admin/organizations` | routes.py:137 | Cognito JWT + admin | yes | 401 | Create organization |
| 35 | GET | `/admin/organizations` | routes.py:152 | Cognito JWT + admin | yes | 401 | List organizations |
| 36 | GET | `/admin/organizations/{org_id}` | routes.py:181 | Cognito JWT + admin | yes | 401 | Get organization |
| 37 | PUT | `/admin/organizations/{org_id}` | routes.py:193 | Cognito JWT + admin | yes | 401 | Update organization |
| 38 | DELETE | `/admin/organizations/{org_id}` | routes.py:206 | Cognito JWT + admin | yes | 401 | Delete organization |
| 39 | GET | `/admin/organizations/{org_id}/budget/{entity_type}/{entity_id}` | routes.py:224 | Cognito JWT + admin | yes | 401 | Get budget config |
| 40 | GET | `/admin/organizations/{org_id}/budget/{entity_type}/{entity_id}/status` | routes.py:238 | Cognito JWT + admin | yes | 401 | Get budget status |
| 41 | PUT | `/admin/organizations/{org_id}/budget/{entity_type}/{entity_id}` | routes.py:255 | Cognito JWT + admin | yes | 401 | Update budget config |
| 42 | GET | `/admin/organizations/{org_id}/budgets` | routes.py:273 | Cognito JWT + admin | yes | 401 | List all budgets |
| 43 | POST | `/admin/organizations/{org_id}/budgets` | routes.py:293 | Cognito JWT + admin | yes | 401 | Create budget |
| 44 | DELETE | `/admin/organizations/{org_id}/budget/{entity_type}/{entity_id}/{period_type}` | routes.py:309 | Cognito JWT + admin | yes | 401 | Delete budget |
| 45 | GET | `/admin/organizations/{org_id}/ratelimit/{entity_type}/{entity_id}` | routes.py:330 | Cognito JWT + admin | yes | 401 | Get rate limit config |
| 46 | PUT | `/admin/organizations/{org_id}/ratelimit/{entity_type}/{entity_id}` | routes.py:344 | Cognito JWT + admin | yes | 401 | Update rate limit |
| 47 | GET | `/admin/organizations/{org_id}/ratelimits` | routes.py:362 | Cognito JWT + admin | yes | 401 | List rate limits |
| 48 | POST | `/admin/organizations/{org_id}/ratelimits` | routes.py:381 | Cognito JWT + admin | yes | 401 | Create rate limit |
| 49 | DELETE | `/admin/organizations/{org_id}/ratelimit/{entity_type}/{entity_id}` | routes.py:397 | Cognito JWT + admin | yes | 401 | Delete rate limit |
| 50 | GET | `/admin/pool/status` | routes.py:417 | Cognito JWT + admin | yes | 401 | Pool status |
| 51 | POST | `/admin/pool/accounts` | routes.py:431 | Cognito JWT + admin | yes | 401 | Add pool account |
| 52 | DELETE | `/admin/pool/accounts/{account_id}` | routes.py:446 | Cognito JWT + admin | yes | 401 | Remove pool account |
| 53 | GET | `/admin/logs` | routes.py:464 | Cognito JWT + admin | yes | 401 | Query audit logs |
| 54 | GET | `/admin/dashboard/platform` | routes.py:530 | Cognito JWT + admin | yes | 401 | Platform dashboard |
| 55 | GET | `/admin/dashboard/org/{org_id}` | routes.py:566 | Cognito JWT + admin | yes | 401 | Org dashboard |
| 56 | POST | `/admin/organizations/{org_id}/departments` | routes.py:601 | Cognito JWT + admin | yes | 401 | Create department |
| 57 | GET | `/admin/organizations/{org_id}/departments` | routes.py:618 | Cognito JWT + admin | yes | 401 | List departments |
| 58 | GET | `/admin/organizations/{org_id}/departments/{dept_id}` | routes.py:641 | Cognito JWT + admin | yes | 401 | Get department |
| 59 | PUT | `/admin/organizations/{org_id}/departments/{dept_id}` | routes.py:654 | Cognito JWT + admin | yes | 401 | Update department |
| 60 | DELETE | `/admin/organizations/{org_id}/departments/{dept_id}` | routes.py:668 | Cognito JWT + admin | yes | 401 | Delete department |
| 61 | POST | `/admin/organizations/{org_id}/departments/{dept_id}/teams` | routes.py:685 | Cognito JWT + admin | yes | 401 | Create team |
| 62 | GET | `/admin/organizations/{org_id}/departments/{dept_id}/teams` | routes.py:702 | Cognito JWT + admin | yes | 401 | List teams |
| 63 | PUT | `/admin/organizations/{org_id}/teams/{team_id}` | routes.py:726 | Cognito JWT + admin | yes | 401 | Update team |
| 64 | DELETE | `/admin/organizations/{org_id}/teams/{team_id}` | routes.py:740 | Cognito JWT + admin | yes | 401 | Delete team |
| 65 | POST | `/admin/organizations/{org_id}/teams/{team_id}/users` | routes.py:756 | Cognito JWT + admin | yes | 401 | Add user to team |
| 66 | GET | `/admin/organizations/{org_id}/users` | routes.py:775 | Cognito JWT + admin | yes | 401 | List org users |
| 67 | GET | `/admin/organizations/{org_id}/teams/{team_id}/users` | routes.py:798 | Cognito JWT + admin | yes | 401 | List team users |
| 68 | DELETE | `/admin/organizations/{org_id}/users/{user_id}` | routes.py:822 | Cognito JWT + admin | yes | 401 | Remove user |
| 69 | POST | `/admin/organizations/{org_id}/service-accounts` | routes.py:843 | Cognito JWT + admin | yes | 401 | Create org service account |
| 70 | GET | `/admin/organizations/{org_id}/service-accounts` | routes.py:861 | Cognito JWT + admin | yes | 401 | List org service accounts |
| 71 | DELETE | `/admin/organizations/{org_id}/service-accounts/{sa_id}` | routes.py:884 | Cognito JWT + admin | yes | 401 | Delete org service account |
| 72 | POST | `/admin/agents` | routes.py:910 | Cognito JWT + admin | yes | 401 | Create agent (client_credentials) |
| 73 | GET | `/admin/agents` | routes.py:928 | Cognito JWT + admin | yes | 401 | List agents |
| 74 | GET | `/admin/agents/{client_id}` | routes.py:953 | Cognito JWT + admin | yes | 401 | Get agent details |
| 75 | GET | `/admin/agents/{client_id}/credentials` | routes.py:970 | Cognito JWT + admin | yes | 401 | Get agent credentials |
| 76 | PUT | `/admin/agents/{client_id}` | routes.py:993 | Cognito JWT + admin | yes | 401 | Update agent |
| 77 | DELETE | `/admin/agents/{client_id}` | routes.py:1014 | Cognito JWT + admin | yes | 401 | Delete agent |
| 78 | GET | `/admin/users/roles` | routes.py:1039 | Cognito JWT | yes | 401 | Get user roles |
| 79 | GET | `/admin/organizations/{org_id}/usage/timeseries` | routes.py:1060 | Cognito JWT + admin | yes | 401 | Usage timeseries |
| 80 | GET | `/admin/users/me/chats` | routes.py:1101 | Cognito JWT | yes | 401 | List user's chats |
| 81 | GET | `/admin/users/me/chats/{request_id}` | routes.py:1139 | Cognito JWT | yes | 401 | Get chat detail |
| 82 | GET | `/admin/organizations/{org_id}/cognito/users` | routes.py:1208 | Cognito JWT + admin | yes | 401 | Cognito user sync |
| 83 | GET | `/admin/organizations/{org_id}/cognito/teams` | routes.py:1248 | Cognito JWT + admin | yes | 401 | Cognito team sync |
| 84 | GET | `/admin/organizations/{org_id}/cognito/departments` | routes.py:1295 | Cognito JWT + admin | yes | 401 | Cognito dept sync |
| 85 | POST | `/admin/registry/agents` | routes.py:1343 | Cognito JWT + org admin | yes | 401 | Register agent (DynamoDB) |
| 86 | GET | `/admin/registry/agents` | routes.py:1367 | Cognito JWT + org admin | yes | 401 | List registry agents |
| 87 | GET | `/admin/registry/agents/{agent_id}` | routes.py:1406 | Cognito JWT + org admin | yes | 401 | Get registry agent |
| 88 | PATCH | `/admin/registry/agents/{agent_id}` | routes.py:1426 | Cognito JWT + org admin | yes | 401 | Update registry agent |
| 89 | DELETE | `/admin/registry/agents/{agent_id}` | routes.py:1450 | Cognito JWT + org admin | yes | 401 | Delete (disable) registry agent |
| 90 | GET | `/admin/registry/agents/{agent_id}/usage` | routes.py:1478 | Cognito JWT + org admin | yes | 401 | Agent usage data |
| 91 | POST | `/admin/agents/onboard` | routes.py:1527 | Cognito JWT | yes | 401 | Agent onboarding orchestrator |
| 92 | POST | `/admin/policies/preview` | routes.py:1568 | Cognito JWT + admin | yes | 401 | Preview IAM policies |
| 93 | GET | `/admin/policies/agent-types` | routes.py:1678 | Cognito JWT | yes | 401 | List agent types |

### Health Endpoints (src/admin/health.py) — NOT routed via CloudFront

| # | Method | Path | Line | Auth | CF Routed | Notes |
|---|--------|------|------|------|-----------|-------|
| 94 | GET | `/health` | admin/health.py:148 | none | **NO** (shadowed by app.py:180 root `/health`) | Detailed health check |
| 95 | GET | `/ready` | admin/health.py:160 | none | **NO** (shadowed by app.py:185 root `/ready`) | Detailed readiness |
| 96 | GET | `/health/detailed` | admin/health.py:174 | none | **NO** (S3 fallback) | DB/Redis/S3 connectivity |

**Note**: The `admin/health.py` router has no prefix, so its `/health` and `/ready` endpoints conflict with (and are shadowed by) the app-level definitions in `app.py:180-186`. The `/health/detailed` endpoint is unique but not routed by CloudFront.

### Identity Admin Endpoints (src/admin/identity/router.py) — NOT routed via CloudFront

Prefix: `/api/admin/identity` — note the double `/api/` when accessed via CloudFront (`/api/api/admin/identity/...`)

| # | Method | Path | Line | Auth | CF Routed | Notes |
|---|--------|------|------|------|-----------|-------|
| 97 | POST | `/api/admin/identity/organizations` | router.py:47 | require_admin | **NO** | Create org (identity layer) |
| 98 | GET | `/api/admin/identity/organizations` | router.py:62 | require_admin | **NO** | List orgs |
| 99 | GET | `/api/admin/identity/organizations/{org_id}` | router.py:73 | require_admin | **NO** | Get org |
| 100 | PATCH | `/api/admin/identity/organizations/{org_id}` | router.py:87 | require_admin | **NO** | Update org |
| 101 | DELETE | `/api/admin/identity/organizations/{org_id}` | router.py:102 | require_admin | **NO** | Delete org |
| 102 | POST | `/api/admin/identity/organizations/{org_id}/users` | router.py:120 | require_admin | **NO** | Create user |
| 103 | GET | `/api/admin/identity/organizations/{org_id}/users` | router.py:136 | require_admin | **NO** | List users |
| 104 | DELETE | `/api/admin/identity/organizations/{org_id}/users/{user_id}` | router.py:148 | require_admin | **NO** | Delete user |
| 105 | POST | `/api/admin/identity/users/{user_id}/identities` | router.py:167 | require_admin | **NO** | Create identity |
| 106 | GET | `/api/admin/identity/users/{user_id}/identities` | router.py:182 | require_admin | **NO** | List identities |
| 107 | DELETE | `/api/admin/identity/users/{user_id}/identities/{identity_id}` | router.py:194 | require_admin | **NO** | Delete identity |

### Connections Endpoints (src/admin/connections/routes.py) — NOT routed via CloudFront

Prefix: `/api/admin/connections`

| # | Method | Path | Line | Auth | CF Routed | Notes |
|---|--------|------|------|------|-----------|-------|
| 108 | POST | `/api/admin/connections/github/install-start` | routes.py:79 | require_admin | **NO** | Start GitHub App install |
| 109 | GET | `/api/admin/connections/github/install-callback` | routes.py:99 | none (OAuth callback) | **NO** | GitHub install callback |
| 110 | GET | `/api/admin/connections` | routes.py:149 | Cognito JWT | **NO** | List connections |
| 111 | DELETE | `/api/admin/connections/github/{installation_id}` | routes.py:169 | require_admin | **NO** | Delete connection |

### Onboarding Endpoints (src/admin/onboarding/handler.py) — NOT routed via CloudFront

No prefix on router (bare `APIRouter()`).

| # | Method | Path | Line | Auth | CF Routed | Notes |
|---|--------|------|------|------|-----------|-------|
| 112 | GET | `/access/status` | handler.py:438 | Cognito JWT | **NO** | Check access status |
| 113 | POST | `/access/request` | handler.py:466 | Cognito JWT | **NO** | Submit access request |
| 114 | GET | `/admin/access-requests` | handler.py:579 | require_admin | **NO** | List access requests |
| 115 | POST | `/admin/access-requests/{request_id}/approve` | handler.py:606 | require_admin | **NO** | Approve request |
| 116 | POST | `/admin/access-requests/{request_id}/deny` | handler.py:640 | require_admin | **NO** | Deny request |

**IMPORTANT NOTE**: Endpoints 114-116 have paths starting with `/admin/` which SHOULD be routed via CloudFront (the `/admin/*` pattern works for other admin routes). However, live testing shows these go to S3 fallback. This is likely because the onboarding router's `APIRouter()` with no prefix creates bare paths that don't participate in the `/admin` prefix group correctly, OR CloudFront has specific exclusion rules for `/admin/access-requests*`. Needs further investigation.

### Internal Endpoints (src/internal/routes.py) — NOT routed via CloudFront

Prefix: `/internal/v1`

| # | Method | Path | Line | Auth | CF Routed | Notes |
|---|--------|------|------|------|-----------|-------|
| 117 | POST | `/internal/v1/issue-magic-link` | routes.py:142 | X-Internal-Api-Key | **NO** | Issue magic link |
| 118 | POST | `/internal/v1/resolve-user` | routes.py:214 | X-Internal-Api-Key | **NO** | Resolve provider identity |

### Internal Credential Endpoints (src/internal/credential_routes.py) — NOT routed via CloudFront

Prefix: `/internal/v1`

| # | Method | Path | Line | Auth | CF Routed | Notes |
|---|--------|------|------|------|-----------|-------|
| 119 | GET | `/internal/v1/user-credentials` | credential_routes.py:246 | X-Internal-Api-Key / IRSA | **NO** | List credential metadata |
| 120 | POST | `/internal/v1/proxy-request` | credential_routes.py:296 | X-Internal-Api-Key / IRSA + scope | **NO** | HTTP proxy with cred injection |
| 121 | POST | `/internal/v1/credential-materialize` | credential_routes.py:400 | X-Internal-Api-Key / IRSA + scope | **NO** | Materialize file credential |
| 122 | POST | `/internal/v1/credential-raw-read` | credential_routes.py:521 | X-Internal-Api-Key / IRSA + scope + feature flag | **NO** | Raw credential read (dual-gated) |

### Internal Assume Role Endpoint (src/internal/assume_role_routes.py) — NOT routed via CloudFront

| # | Method | Path | Line | Auth | CF Routed | Notes |
|---|--------|------|------|------|-----------|-------|
| 123 | POST | `/internal/v1/credential-assume-role` | assume_role_routes.py:144 | X-Internal-Api-Key / IRSA | **NO** | STS AssumeRole for aws_role creds |

### Internal Provenance Endpoint (src/internal/provenance_routes.py) — NOT routed via CloudFront

| # | Method | Path | Line | Auth | CF Routed | Notes |
|---|--------|------|------|------|-----------|-------|
| 124 | POST | `/internal/v1/provenance` | provenance_routes.py:70 | X-Internal-Api-Key / IRSA | **NO** | Write action provenance row |

### Usage Endpoints (src/usage/routes.py)

Prefix: `/usage`

| # | Method | Path | Line | Auth | CF Routed | Status (unauth) | Notes |
|---|--------|------|------|------|-----------|-----------------|-------|
| 125 | GET | `/usage/summary` | routes.py:52 | **NONE** (bug?) | yes | 200 `{"error":"org_id required"}` | Returns error message, not 401 |
| 126 | GET | `/usage/organizations` | routes.py:97 | **NONE** (bug?) | yes | 200 `[]` | Returns empty list without auth |
| 127 | GET | `/usage/organizations/{org_id}` | routes.py:119 | **NONE** (bug?) | yes | 200 | Returns data without auth |
| 128 | GET | `/usage/models` | routes.py:140 | **NONE** (bug?) | yes | 200 `[]` | Returns empty list without auth |
| 129 | GET | `/usage/timeline` | routes.py:170 | **NONE** (bug?) | yes | 200 (returns timeline data) | Returns data without auth |
| 130 | GET | `/usage/users` | routes.py:200 | **NONE** | yes | 422 (missing org_id) | No auth check |
| 131 | GET | `/usage/departments` | routes.py:224 | **NONE** | yes | 422 (missing org_id) | No auth check |
| 132 | GET | `/usage/logs` | routes.py:247 | **NONE** | yes | 422 (missing org_id) | No auth check |

**SECURITY FINDING**: All `/usage/*` endpoints appear to have NO authentication. They are publicly accessible via CloudFront. While they return empty data for invalid org_ids, if a valid org_id is guessed or known, usage data would be exposed without authentication. This should be filed as a security issue.

### Budget Endpoints (src/budget/routes.py)

Prefix: `/budgets`

| # | Method | Path | Line | Auth | CF Routed | Status (unauth) | Notes |
|---|--------|------|------|------|-----------|-----------------|-------|
| 133 | POST | `/budgets/` | routes.py:47 | Cognito JWT | yes | 401 | Create budget |
| 134 | GET | `/budgets/{budget_id}` | routes.py:62 | Cognito JWT | yes | 401 | Get budget |
| 135 | GET | `/budgets/entity/{entity_type}/{entity_id}` | routes.py:75 | Cognito JWT | yes | 401 | Get budgets by entity |
| 136 | PUT | `/budgets/{budget_id}` | routes.py:86 | Cognito JWT | yes | 401 | Update budget |
| 137 | DELETE | `/budgets/{budget_id}` | routes.py:107 | Cognito JWT | yes | 401 | Delete budget |
| 138 | GET | `/budgets/status/{entity_type}/{entity_id}` | routes.py:122 | Cognito JWT | yes | 401 | Budget status |
| 139 | GET | `/budgets/usage/{entity_type}/{entity_id}` | routes.py:139 | Cognito JWT | yes | 401 | Budget usage |
| 140 | POST | `/budgets/calculate-cost` | routes.py:159 | Cognito JWT | yes | 401 | Calculate cost |
| 141 | POST | `/budgets/record-cost` | routes.py:168 | Cognito JWT | yes | 401 | Record cost |
| 142 | GET | `/budgets/summary/{entity_type}/{entity_id}` | routes.py:184 | Cognito JWT | yes | 401 | Budget summary |
| 143 | GET | `/budgets/organization/overview` | routes.py:195 | Cognito JWT | yes | 401 | Org overview |
| 144 | GET | `/budgets/organization/alerts` | routes.py:204 | Cognito JWT | yes | 401 | Org alerts |

### Rate Limit Endpoints (src/ratelimit/routes.py)

Prefix: `/ratelimits`

| # | Method | Path | Line | Auth | CF Routed | Status (unauth) | Notes |
|---|--------|------|------|------|-----------|-----------------|-------|
| 145 | GET | `/ratelimits` | routes.py:56 | Cognito JWT | yes | 401 | List rate limits |
| 146 | GET | `/ratelimits/{entity_type}/{entity_id}` | routes.py:96 | Cognito JWT | yes | 401 | Get rate limit |
| 147 | PUT | `/ratelimits/{entity_type}/{entity_id}` | routes.py:133 | Cognito JWT + admin | yes | 401 | Update rate limit |
| 148 | DELETE | `/ratelimits/{entity_type}/{entity_id}` | routes.py:164 | Cognito JWT + admin | yes | 401 | Delete rate limit |
| 149 | GET | `/ratelimits/{entity_type}/{entity_id}/status` | routes.py:192 | Cognito JWT | yes | 401 | Rate limit status |

## Per-Endpoint Findings

### POST /v1/messages (CRITICAL — tools dropped)

**Auth**: Cognito JWT (via `Authorization: Bearer <token>` or `X-Api-Key: <token>`)
**Code path**: `routes.py:289` → `proxy_service.messages()` (service.py:231) → `translator.anthropic_to_bedrock()` (format_translator.py:215) → `proxy_service._invoke_anthropic_response()` → Bedrock `invoke_model`

**Test command (oauth)**:
```bash
TOKEN=$(bg-cognito-auth.sh token)
curl -sS https://dp7n42m5j4pl6.cloudfront.net/api/v1/messages \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-sonnet-4-20250514",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "Hello"}],
    "tools": [{"name": "get_weather", "description": "Get weather", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}],
    "tool_choice": {"type": "auto"}
  }'
```

**Test command (iam — from agent pod)**:
```bash
curl -sS http://127.0.0.1:9090/v1/messages \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-sonnet-4-20250514",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "Hello"}],
    "tools": [{"name": "get_weather", "description": "Get weather", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}],
    "tool_choice": {"type": "auto"}
  }'
```

**Code analysis finding**: `format_translator.py:242-263` constructs `BedrockInvokeRequest` with only: `anthropic_version`, `max_tokens`, `messages`, `system`, `temperature`, `top_p`, `top_k`, `stop_sequences`. **Neither `tools` nor `tool_choice` are copied.** The `BedrockInvokeRequest` schema (schemas.py:340-341) supports both fields, but they are never populated in the Anthropic→Bedrock translation path.

**Impact**: Any client using the `/v1/messages` endpoint (Anthropic Messages format) will have their tools silently dropped. Bedrock will receive a request without tools, respond with a text-only message, and the client will never get `tool_use` content blocks. This affects both auth modes identically since the translation is auth-agnostic.

**Divergence between auth modes**: None expected at the translation layer. Both OAuth and IAM paths call the same `proxy_service.messages()` method.

### POST /model/{model_id}/invoke (tools PRESERVED)

**Auth**: Cognito JWT
**Code path**: `routes.py:521` → `body = await request.json()` → `proxy_service.invoke_model(model_id, body, ...)` (service.py:261) → `BedrockInvokeRequest(**body)` (service.py:284)

**Why tools are preserved**: The body is passed through as a dict, and `BedrockInvokeRequest` has `model_config = {"extra": "allow"}` plus explicit `tools`/`tool_choice` fields. No translation occurs — Bedrock receives exactly what the client sent (minus the `model`/`modelId` field which is extracted for routing).

**Test command (oauth)**:
```bash
TOKEN=$(bg-cognito-auth.sh token)
curl -sS https://dp7n42m5j4pl6.cloudfront.net/api/model/us.anthropic.claude-sonnet-4-20250514-v1:0/invoke \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "anthropic_version": "bedrock-2023-05-31",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "What is the weather in Seattle?"}],
    "tools": [{"name": "get_weather", "description": "Get weather for a city", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}],
    "tool_choice": {"type": "auto"}
  }'
```

### POST /v1/chat/completions (tools likely dropped)

**Auth**: Cognito JWT
**Code path**: `routes.py:228` → `proxy_service.chat_completions()` (service.py:201) → `translator.openai_to_bedrock()` (format_translator.py:69)

**Analysis**: The `openai_to_bedrock` method (format_translator.py:69-130) similarly constructs a `BedrockInvokeRequest` with explicit field assignment. Based on the pattern seen in `anthropic_to_bedrock`, tools likely not copied here either (confirmed by absence of "tools" in grep of format_translator.py). Needs authenticated test to confirm runtime behavior.

### GET /usage/summary (SECURITY — no auth)

**Auth**: NONE
**Status**: 200 with `{"error":"org_id required"}` — the endpoint runs without authentication

**Test command**:
```bash
curl -sS https://dp7n42m5j4pl6.cloudfront.net/api/usage/summary
# Returns: {"error":"org_id required"}

curl -sS "https://dp7n42m5j4pl6.cloudfront.net/api/usage/summary?org_id=some-known-org-id"
# Would return actual usage data without authentication
```

**Finding**: All 8 endpoints under `/usage/*` have no authentication dependency. They rely on the caller providing a valid `org_id` but don't verify the caller has permission to see that org's data.

### POST /auth/exchange (deprecated, disabled)

**Auth**: Self-authenticating (accepts AWS credentials in body)
**Status**: 410 Gone (endpoint disabled by default)

**Test command**:
```bash
curl -sS -X POST https://dp7n42m5j4pl6.cloudfront.net/api/auth/exchange \
  -H "Content-Type: application/json" \
  -d '{"aws_access_key_id":"AKIAIOSFODNN7EXAMPLE","aws_secret_access_key":"wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY","aws_session_token":"FwoGZXIvY..."}'
```

**Observed**: Returns 410 with message directing to Cognito JWT auth. Feature flag `BG_ENABLE_LEGACY_AUTH_EXCHANGE` controls this.

## Skip List

| Endpoint | Reason |
|----------|--------|
| All vault endpoints (#23-31) | Not routed via CloudFront; no ALB direct access from this pod |
| All AWS connect endpoints (#32-33) | Not routed via CloudFront |
| All identity admin endpoints (#97-107) | Not routed via CloudFront (prefix collision: `/api/admin/identity/` → `/api/api/admin/identity/` from CloudFront) |
| All connections endpoints (#108-111) | Not routed via CloudFront; GitHub OAuth callback needs browser |
| All onboarding endpoints (#112-116) | Not routed via CloudFront |
| All internal endpoints (#117-124) | Not routed via CloudFront; internal-only by design |
| Health detailed (#96) | Not routed via CloudFront |
| All authenticated endpoints | Agent pod lacks Cognito JWT token |
| All IAM SigV4 endpoints | No sigv4-proxy running in this pod; API Gateway not accessible |

## Issues to File

1. **SECURITY: /usage/* endpoints have no authentication** — All 8 usage endpoints are publicly accessible. Anyone who knows a valid `org_id` can read usage statistics. Endpoints 125-132.

2. **CONFIRMED: /v1/messages drops tools/tool_choice** — The `anthropic_to_bedrock()` translator does not copy these fields. This is the root cause of #790.

3. **ROUTING: /api/admin/identity/* prefix collision** — The identity admin router uses prefix `/api/admin/identity` which, when accessed via CloudFront's `/api/*` strip, becomes `/api/api/admin/identity/` — unreachable. These endpoints are only reachable via direct ALB or API Gateway.

4. **ROUTING: Onboarding handler's `/admin/access-requests*` not routed** — Despite matching the `/admin/*` pattern that works for other admin routes, the onboarding handler's endpoints get S3 fallback. May be a CloudFront behavior ordering issue.

5. **SHADOWED: admin/health.py `/health` and `/ready`** — These endpoints are registered but shadowed by the app-level definitions in `app.py:180-186` (registered earlier). The `/health/detailed` endpoint is unique but unreachable via CloudFront.

## Reproduction Commands

All tests can be re-run from any machine with curl:

```bash
# Public endpoints (no auth needed)
curl -sS https://dp7n42m5j4pl6.cloudfront.net/api/health
curl -sS https://dp7n42m5j4pl6.cloudfront.net/api/ready
curl -sS https://dp7n42m5j4pl6.cloudfront.net/api/v1/health
curl -sS https://dp7n42m5j4pl6.cloudfront.net/api/usage/summary

# Auth-required endpoints (expect 401)
curl -sS https://dp7n42m5j4pl6.cloudfront.net/api/v1/models
curl -sS https://dp7n42m5j4pl6.cloudfront.net/api/admin/organizations

# Verify endpoint routing (check server header)
curl -sI https://dp7n42m5j4pl6.cloudfront.net/api/auth/me | grep server
# Expected: server: uvicorn

curl -sI https://dp7n42m5j4pl6.cloudfront.net/api/auth/credentials | grep server
# Expected: server: AmazonS3 (not routed to gateway)
```

## Next Steps

1. Re-run this audit with authenticated requests (both OAuth and IAM SigV4) to verify:
   - Actual response payloads for proxy endpoints
   - tools/tool_choice behavior at runtime (not just code analysis)
   - Tenant isolation (can org A see org B's data?)
2. File security issue for `/usage/*` unauthenticated endpoints
3. Fix tools/tool_choice in `format_translator.py:anthropic_to_bedrock()` (tracked by #790)
4. Investigate CloudFront behavior rules for the routing gaps
