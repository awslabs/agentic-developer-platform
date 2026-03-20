# Bedrock Gateway Sequence Diagrams

This document contains Mermaid sequence diagrams for key user flows in Bedrock Gateway.

## Table of Contents

1. [Human Developer Using Claude Code](#1-human-developer-using-claude-code)
2. [Agent M2M Authentication](#2-agent-m2m-authentication)
3. [Admin UI Login](#3-admin-ui-login)
4. [Create Organization](#4-create-organization)
5. [Budget Enforcement](#5-budget-enforcement)
6. [Rate Limiting](#6-rate-limiting)

---

## 1. Human Developer Using Claude Code

This flow shows how a developer authenticates via Cognito and uses Claude Code with the Bedrock Gateway.

```mermaid
sequenceDiagram
    autonumber
    participant Dev as Developer
    participant CLI as bg-cognito-auth.sh
    participant Cognito as Cognito User Pool
    participant IdPool as Cognito Identity Pool
    participant Claude as Claude Code
    participant CF as CloudFront
    participant GW as Bedrock Gateway (EKS)
    participant Bedrock as Amazon Bedrock

    Note over Dev,CLI: Initial Setup (One-time)
    Dev->>CLI: ./bg-cognito-auth.sh login --gateway-url https://gw.company.com

    CLI->>Dev: Prompt for username/password
    Dev->>CLI: Enter credentials

    CLI->>Cognito: POST initiate-auth (USER_PASSWORD_AUTH)
    Cognito-->>CLI: ID Token, Access Token, Refresh Token

    CLI->>IdPool: get-id (with ID Token)
    IdPool-->>CLI: Identity ID

    CLI->>IdPool: get-credentials-for-identity
    IdPool-->>CLI: AWS Temp Credentials (AccessKey, SecretKey, SessionToken)

    CLI->>CLI: Write credentials to ~/.aws/credentials [bedrock-gateway]
    CLI-->>Dev: Success! Setup complete

    Note over Dev,Claude: Using Claude Code
    Dev->>Claude: Start Claude Code with CLAUDE_CODE_USE_BEDROCK=1

    Claude->>Claude: Load credentials from ~/.aws/credentials

    Claude->>CF: POST /api/v1/chat/completions (SigV4 signed)
    Note over CF: CloudFront strips /api prefix

    CF->>GW: POST /v1/chat/completions (via VPC Origin)

    GW->>GW: Validate SigV4 signature
    GW->>GW: Extract identity from Cognito session tags
    GW->>GW: Check budget (org -> dept -> team -> user)
    GW->>GW: Check rate limits

    GW->>Bedrock: InvokeModelWithResponseStream (SigV4)
    Note over Bedrock: Using Gateway's IRSA credentials

    Bedrock-->>GW: Streaming response chunks

    loop For each chunk
        GW-->>CF: SSE data event
        CF-->>Claude: SSE data event
    end

    GW->>GW: Log usage, update budget

    Claude-->>Dev: Display response
```

### Key Points

- **CLI handles all token management**: Developers don't manually copy tokens
- **Credentials stored in AWS profile**: `~/.aws/credentials` under `[bedrock-gateway]`
- **CloudFront VPC Origin**: ALB is internal, CloudFront is the only ingress
- **SigV4 authentication**: Gateway validates AWS credentials via STS
- **Streaming support**: SSE works through CloudFront with extended timeouts

---

## 2. Agent M2M Authentication

This flow shows how automated agents (CI/CD pipelines, EKS containers) authenticate using Cognito's `client_credentials` flow.

```mermaid
sequenceDiagram
    autonumber
    participant Agent as Agent (EKS Pod / CI Job)
    participant Cognito as Cognito Token Endpoint
    participant CF as CloudFront
    participant GW as Bedrock Gateway
    participant DB as PostgreSQL
    participant Bedrock as Amazon Bedrock

    Note over Agent,Cognito: Agent Setup (Admin creates App Client)
    Note over Agent: Agent has client_id and client_secret as env vars

    Agent->>Cognito: POST /oauth2/token (client_credentials grant)
    Note over Agent,Cognito: client_id, client_secret, scope=bedrockgw/invoke

    Cognito-->>Agent: Access Token (JWT)
    Note over Agent: Token contains custom claims: org_id, team_id, scope

    Note over Agent,Bedrock: Make API Request
    Agent->>CF: POST /api/v1/chat/completions
    Note over Agent,CF: Authorization: Bearer <access_token>

    CF->>GW: Forward to VPC Origin (strip /api)

    GW->>GW: Extract JWT from Authorization header
    GW->>GW: Validate JWT signature (cached JWKS)
    GW->>GW: Extract custom claims (org_id, team_id, etc.)

    GW->>DB: Lookup organization settings
    DB-->>GW: Org config, model permissions

    GW->>GW: Check budget for service account
    GW->>GW: Check rate limits (separate from human users)

    GW->>Bedrock: InvokeModelWithResponseStream
    Bedrock-->>GW: Response stream

    GW-->>CF: Forward response
    CF-->>Agent: Response

    GW->>DB: Log usage (account_type: 'service')
```

### Key Points

- **Client credentials flow**: No user interaction required
- **App Client per agent**: Admin creates in Cognito or via `/admin/agents` API
- **JWT validation via JWKS**: No STS calls, faster validation
- **Separate quotas**: Service accounts have independent budgets/rate limits
- **Usage tracking**: Logged with `account_type: service` for reporting

---

## 3. Admin UI Login

This flow shows the Admin UI authentication using Cognito Hosted UI with PKCE.

```mermaid
sequenceDiagram
    autonumber
    participant Admin as Org Admin
    participant Browser as Browser
    participant CF as CloudFront
    participant S3 as S3 (Admin UI)
    participant Cognito as Cognito Hosted UI
    participant IdP as Corporate IdP
    participant GW as Bedrock Gateway

    Admin->>Browser: Navigate to https://gateway.company.com/admin

    Browser->>CF: GET /admin
    CF->>S3: Fetch index.html
    S3-->>CF: React SPA
    CF-->>Browser: Admin UI SPA

    Browser->>Browser: Check for valid token in localStorage
    Note over Browser: No token found, redirect to login

    Browser->>Browser: Generate PKCE code_verifier, code_challenge

    Browser->>Cognito: GET /oauth2/authorize
    Note over Browser,Cognito: response_type=code, client_id, redirect_uri<br/>code_challenge, code_challenge_method=S256

    Cognito->>Cognito: Check authentication status
    Note over Cognito: User not authenticated

    Cognito-->>Browser: Redirect to IdP login page

    Browser->>IdP: Display login page
    Admin->>IdP: Enter corporate credentials

    IdP->>IdP: Validate credentials
    IdP-->>Cognito: SAML assertion / OIDC token

    Cognito->>Cognito: Map IdP claims to Cognito attributes
    Note over Cognito: Extract: org_id, team_id, role (admin flag)

    Cognito-->>Browser: Redirect to callback with auth code

    Browser->>Cognito: POST /oauth2/token
    Note over Browser,Cognito: grant_type=authorization_code<br/>code, redirect_uri, code_verifier

    Cognito-->>Browser: ID Token, Access Token, Refresh Token

    Browser->>Browser: Store tokens in localStorage

    Browser->>CF: GET /api/admin/dashboard/org/{org_id}
    Note over Browser,CF: Authorization: Bearer <access_token>

    CF->>GW: Forward to Gateway

    GW->>GW: Validate JWT
    GW->>GW: Check admin role claim
    GW-->>CF: Dashboard data
    CF-->>Browser: Dashboard data

    Browser-->>Admin: Display Admin Dashboard
```

### Key Points

- **PKCE flow**: Secure OAuth 2.0 for single-page applications
- **Cognito Hosted UI**: Handles IdP federation (Okta, Azure AD, Auth0)
- **Token storage**: Access token stored in localStorage
- **Admin role**: Extracted from JWT `custom:role` claim
- **Silent refresh**: Browser refreshes tokens before expiry

---

## 4. Create Organization

This flow shows how a platform admin onboards a new organization.

```mermaid
sequenceDiagram
    autonumber
    participant Admin as Platform Admin
    participant UI as Admin UI
    participant CF as CloudFront
    participant GW as Bedrock Gateway
    participant DB as PostgreSQL
    participant Cognito as Cognito User Pool

    Admin->>UI: Click "Add Organization"

    UI->>UI: Display organization form
    Admin->>UI: Fill form (name, AWS accounts, role mappings)

    UI->>CF: POST /api/admin/organizations
    Note over UI,CF: Authorization: Bearer <admin_token><br/>Body: { name, aws_accounts, role_mappings }

    CF->>GW: Forward request

    GW->>GW: Validate admin JWT
    GW->>GW: Check platform_admin role claim

    GW->>DB: Check for duplicate org name
    DB-->>GW: No duplicates

    GW->>DB: Check for overlapping AWS accounts
    DB-->>GW: No overlaps

    GW->>Cognito: CreateGroup (org-{org_id})
    Cognito-->>GW: Group created

    GW->>DB: INSERT organization
    Note over DB: Store: id, name, aws_accounts,<br/>role_mappings, settings, created_at

    DB-->>GW: Organization created

    GW->>DB: Create default budget config
    GW->>DB: Create default rate limit config

    GW-->>CF: 201 Created { org_id, name, ... }
    CF-->>UI: Response

    UI-->>Admin: Show success message + org details

    Note over Admin,UI: Admin can now add departments, teams, users
```

### Key Points

- **Platform admin only**: Requires `custom:role = platform_admin` claim
- **Validation**: Checks for duplicate names and overlapping AWS accounts
- **Cognito group**: Created for org-level IAM role mapping
- **Default configs**: Org gets default budget and rate limit settings
- **Audit logging**: All admin actions logged with actor and timestamp

---

## 5. Budget Enforcement

This flow shows how budget limits are enforced at all hierarchy levels.

```mermaid
sequenceDiagram
    autonumber
    participant Client as Client (Claude Code / Agent)
    participant CF as CloudFront
    participant GW as Bedrock Gateway
    participant DB as PostgreSQL
    participant Bedrock as Amazon Bedrock

    Client->>CF: POST /api/v1/chat/completions
    CF->>GW: Forward request

    GW->>GW: Authenticate (SigV4 or JWT)
    GW->>GW: Extract: org_id, dept_id, team_id, user_id

    Note over GW,DB: Check budgets at all levels

    GW->>DB: SELECT budget_config WHERE entity=user
    DB-->>GW: User budget: $100/month, hard limit

    GW->>DB: SELECT budget_usage WHERE entity=user, period=current_month
    DB-->>GW: User spent: $75.50

    alt User budget exceeded (hard)
        GW-->>CF: 429 Too Many Requests
        Note over GW,CF: { error: "budget_exceeded",<br/>level: "user",<br/>budget_usd: 100,<br/>spent_usd: 102.30,<br/>resets_at: "2026-03-01" }
        CF-->>Client: 429 + Retry-After header
    end

    GW->>DB: SELECT budget_config WHERE entity=team
    DB-->>GW: Team budget: $500/month, hard limit

    GW->>DB: SELECT budget_usage WHERE entity=team, period=current_month
    DB-->>GW: Team spent: $450.00

    alt Team budget exceeded (soft)
        Note over GW: Soft limit - add warning, continue
        GW->>GW: Add X-Budget-Warning header
    end

    GW->>DB: SELECT budget_config WHERE entity=department
    GW->>DB: SELECT budget_config WHERE entity=organization

    Note over GW: All budgets OK (or soft warnings)

    GW->>Bedrock: InvokeModelWithResponseStream
    Bedrock-->>GW: Response + usage (tokens_in, tokens_out)

    GW->>GW: Calculate cost from model pricing
    Note over GW: cost = (tokens_in * input_price) +<br/>(tokens_out * output_price)

    par Update all budget usage levels
        GW->>DB: UPDATE budget_usage SET total_cost += cost WHERE entity=user
        GW->>DB: UPDATE budget_usage SET total_cost += cost WHERE entity=team
        GW->>DB: UPDATE budget_usage SET total_cost += cost WHERE entity=dept
        GW->>DB: UPDATE budget_usage SET total_cost += cost WHERE entity=org
    end

    GW-->>CF: Response with budget headers
    Note over GW,CF: X-Budget-Remaining-USD: 24.50<br/>X-Budget-Period: monthly<br/>X-Budget-Warning: team_soft_limit_exceeded

    CF-->>Client: Response
```

### Key Points

- **Cascading check**: User -> Team -> Department -> Organization
- **Hard vs soft limits**: Hard blocks, soft warns in headers
- **Pre-check**: Budget checked BEFORE calling Bedrock
- **Post-update**: Usage updated AFTER successful response
- **Headers**: Client receives budget status in response headers

---

## 6. Rate Limiting

This flow shows the token bucket rate limiting algorithm.

```mermaid
sequenceDiagram
    autonumber
    participant Client as Client
    participant CF as CloudFront
    participant GW as Bedrock Gateway
    participant Redis as Redis (Optional)
    participant Bedrock as Amazon Bedrock

    Client->>CF: POST /api/v1/chat/completions
    CF->>GW: Forward request

    GW->>GW: Authenticate, extract entity IDs

    Note over GW,Redis: Check rate limits (most restrictive wins)

    alt Using Redis (multi-instance)
        GW->>Redis: GET rate_bucket:user:{user_id}:rpm
        Redis-->>GW: { tokens: 8, last_refill: timestamp }
    else In-memory (single instance)
        GW->>GW: Check in-memory token bucket
    end

    GW->>GW: Refill tokens based on elapsed time
    Note over GW: tokens += (now - last_refill) * refill_rate<br/>tokens = min(tokens, max_capacity)

    alt Insufficient tokens (rate limited)
        GW->>GW: Calculate retry_after from refill rate
        GW-->>CF: 429 Too Many Requests
        Note over GW,CF: { error: "rate_limited",<br/>type: "rpm",<br/>limit: 100,<br/>retry_after_seconds: 5 }
        CF-->>Client: 429 + headers
        Note over CF,Client: Retry-After: 5<br/>X-RateLimit-Limit: 100<br/>X-RateLimit-Remaining: 0<br/>X-RateLimit-Reset: timestamp
    end

    Note over GW: Sufficient tokens, consume 1

    GW->>GW: Decrement token count

    alt Using Redis
        GW->>Redis: SET rate_bucket:user:{user_id}:rpm { tokens: 7, ... }
    else In-memory
        GW->>GW: Update in-memory bucket
    end

    Note over GW: Check concurrent request limit
    GW->>GW: Increment concurrent count for user

    alt Too many concurrent requests
        GW-->>CF: 429 { error: "too_many_concurrent_requests" }
        CF-->>Client: 429
    end

    GW->>Bedrock: InvokeModelWithResponseStream

    Note over GW,Bedrock: For TPM (tokens per minute), consume<br/>tokens AFTER response received

    Bedrock-->>GW: Response + usage

    GW->>GW: Consume TPM tokens based on actual usage
    GW->>GW: Decrement concurrent count

    GW-->>CF: Response with rate limit headers
    Note over GW,CF: X-RateLimit-Limit: 100<br/>X-RateLimit-Remaining: 7<br/>X-RateLimit-Reset: timestamp

    CF-->>Client: Response
```

### Key Points

- **Token bucket algorithm**: Smooth rate limiting with burst capacity
- **Multiple limit types**: RPM, TPM, and concurrent requests
- **Hierarchy check**: User -> Team -> Department -> Org limits
- **Redis optional**: In-memory for single instance, Redis for distributed
- **TPM consumption**: Tokens consumed AFTER response (based on actual usage)
- **Standard headers**: `X-RateLimit-*` headers for client visibility

---

## 7. Request Timing Instrumentation (Issue #144)

This flow shows where timing is measured at each stage of a proxy request.

```mermaid
sequenceDiagram
    autonumber
    participant Client as Client
    participant CF as CloudFront
    participant LM as LoggingMiddleware
    participant Auth as Auth Dependency
    participant Budget as BudgetMiddleware
    participant RL as RateLimitMiddleware
    participant Route as Proxy Route Handler
    participant Bedrock as Amazon Bedrock

    Client->>CF: POST /api/model/{id}/invoke
    CF->>LM: Forward request

    Note over LM: ⏱ Start: total timer<br/>Initialize request.state.timings = {}

    LM->>Budget: dispatch(request, call_next)

    Note over Budget: ⏱ Start: budget_check
    Budget->>Budget: Parse body, check hierarchy
    Note over Budget: ⏱ End: budget_check

    Budget->>RL: call_next(request)

    Note over RL: ⏱ Start: ratelimit_check
    RL->>RL: Check token bucket
    Note over RL: ⏱ End: ratelimit_check

    RL->>Route: call_next(request)

    Note over Auth: ⏱ Start: auth
    Route->>Auth: get_token_context (Cognito JWT)
    Auth-->>Route: TokenContext
    Note over Auth: ⏱ End: auth

    Note over Route: ⏱ Start: bedrock
    Route->>Bedrock: invoke_model
    Bedrock-->>Route: Response
    Note over Route: ⏱ End: bedrock

    Note over Route: ⏱ Start: serialize
    Route->>Route: json.dumps(response)
    Note over Route: ⏱ End: serialize

    Route-->>RL: Response
    RL-->>Budget: Response
    Budget-->>LM: Response

    Note over LM: ⏱ End: total timer
    LM->>LM: Add X-Gateway-Timing header
    Note over LM: auth=5ms;budget_check=12ms;<br/>ratelimit_check=3ms;bedrock=1847ms;<br/>serialize=2ms;total=1870ms

    LM-->>CF: Response with timing header
    CF-->>Client: Response
```

### Key Points

- **Timing initialization**: LoggingMiddleware initializes `request.state.timings = {}` at request start
- **Per-segment timing**: Each middleware/handler records its own segment using `get_timings(request).time_segment(name)`
- **Total timing**: LoggingMiddleware records `total` after all processing completes
- **Header format**: `X-Gateway-Timing: auth=5ms;budget_check=12ms;bedrock=1847ms;total=1870ms`
- **Structured logging**: Timing dict included in JSON log entry for each request
- **Streaming**: For streaming responses, `bedrock_ttfb` (time to first byte) is used instead of `bedrock`

---

## Summary

| Flow | Authentication | Key Service |
|------|---------------|-------------|
| Human Developer | Cognito + SigV4 | bg-cognito-auth.sh CLI |
| Agent M2M | Cognito client_credentials | JWT Bearer |
| Admin UI | Cognito PKCE | Hosted UI + React SPA |
| Create Org | Admin JWT | Admin API |
| Budget Enforcement | Any | BudgetService |
| Rate Limiting | Any | RateLimitService |
| Request Timing | N/A | LoggingMiddleware + get_timings() |
