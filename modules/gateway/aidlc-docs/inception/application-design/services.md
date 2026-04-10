# Service Layer Design

## Service Orchestration

The FastAPI application wires services together via dependency injection. Middleware handles cross-cutting concerns (auth, rate limiting, budget checks) before requests reach route handlers.

## Request Flow (Proxy Request)

```
Client Request
    |
    v
[TenantContextMiddleware] -- extracts org_id from token, sets request context
    |
    v
[AuthMiddleware] -- validates token via AuthService.validate_token()
    |
    v
[RateLimitMiddleware] -- checks via RateLimitService.check_rate_limit()
    |                     returns 429 if exceeded
    v
[BudgetMiddleware] -- checks via BudgetService.check_budget()
    |                  returns 429 if hard limit exceeded
    |                  adds warning header if soft limit exceeded
    v
[Route Handler] -- ProxyService.invoke() or invoke_stream()
    |                  |
    |                  +-- ModelResolver.resolve() + is_allowed()
    |                  +-- PoolService.get_client()
    |                  +-- FormatTranslator (if needed)
    |                  +-- Bedrock API call
    |                  +-- FormatTranslator (response)
    |
    v
[LoggingMiddleware] -- UsageService.log_request()
    |                   BudgetService.record_usage()
    |                   MetricsService.record_request()
    |                   RateLimitService.release_concurrent()
    v
Client Response
```

## Request Flow (Auth — Dual Mode)

### SigV4 Auth (Claude Code, SDKs, Service Accounts)
```
Client Request: SigV4-signed request to any endpoint
    |
    v
[AuthMiddleware] -- detects SigV4 Authorization header
    |                  |
    |                  +-- STS GetCallerIdentity (validate creds)
    |                  +-- TenantResolver.resolve() (extract org/dept/team from role session)
    |                  +-- Set request context (org_id, team_id, user_id)
    |
    v
[Route Handler] -- request proceeds with tenant context
```

### OIDC Bearer Auth (Admin UI, Custom Apps)
```
Client Request: Authorization: Bearer <Cognito JWT>
    |
    v
[AuthMiddleware] -- detects Bearer token
    |                  |
    |                  +-- Validate JWT signature via Cognito JWKS (cached)
    |                  +-- Check token expiry
    |                  +-- Extract org_id, dept_id, team_id, role from claims
    |                  +-- Set request context
    |
    v
[Route Handler] -- request proceeds with tenant context
```

Note: The gateway no longer creates or stores its own tokens. Auth is stateless — every request is validated independently via STS (SigV4) or JWKS (OIDC).

## Request Flow (Admin API)

```
Client Request: /admin/*
    |
    v
[AuthMiddleware] -- validates token
    |
    v
[AccessControl] -- checks admin role (platform/org/dept)
    |                returns 403 if insufficient
    v
[Route Handler] -- ConfigService / BudgetService / UsageService
    |
    v
Client Response
```

## Dependency Injection (FastAPI)

```python
# Services are created at startup and injected via FastAPI Depends()

app.state.auth_service = AuthService(token_repo, sts_client, tenant_resolver)
app.state.proxy_service = ProxyService(pool_service, format_translator, model_resolver)
app.state.budget_service = BudgetService(budget_repo, pricing_table)
app.state.rate_limit_service = RateLimitService(backend)  # InMemory or Redis
app.state.pool_service = PoolService(pool_config, sts_client)
app.state.usage_service = UsageService(usage_repo)
app.state.metrics_service = MetricsService()

# Middleware accesses services via request.app.state
```

## Middleware Chain Order

1. **LoggingMiddleware** (outermost — captures all requests including errors)
2. **TenantContextMiddleware** (extracts tenant from token, sets request state)
3. **AuthMiddleware** (validates token — skips /auth/exchange, /health, /metrics)
4. **RateLimitMiddleware** (checks rate limits — skips non-proxy routes)
5. **BudgetMiddleware** (checks budgets — skips non-proxy routes)

## Database Access Pattern

All database access goes through Repository classes:
- `OrgRepository` — CRUD for organizations, departments, teams, users
- `TokenRepository` — token storage, lookup by hash, cleanup expired
- `BudgetRepository` — budget config and usage CRUD, aggregation queries
- `UsageRepository` — log insertion, filtered queries, summary aggregation
- `ConfigRepository` — rate limit config, model aliases, pool config

Repositories use SQLAlchemy async sessions. All queries include `org_id` filter for tenant isolation.
