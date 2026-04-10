# Component Methods

## Shared Foundation (`src/shared/`)

### Database Models (SQLAlchemy)
- `Organization` — id, name, aws_accounts (JSON), role_mappings (JSON), settings (JSON), created_at
- `Department` — id, org_id (FK), name, identity_center_group_id, synced_at
- `Team` — id, org_id (FK), department_id (FK), name, identity_center_group_id, synced_at
- `User` — id, org_id (FK), team_id (FK), email, identity_center_user_id, synced_at
- `ServiceAccount` — id, org_id (FK), department_id (FK), team_id (FK), name, iam_role_arn, created_at
- `Token` — id, token_hash, entity_type (user/service_account), entity_id, org_id, team_id, department_id, created_at, expires_at, revoked_at
- `BudgetConfig` — id, entity_type, entity_id, org_id, period_type, budget_amount_usd, enforcement_mode, updated_at
- `BudgetUsage` — id, entity_type, entity_id, org_id, period_start, period_type, total_cost_usd, total_tokens, request_count
- `RateLimitConfig` — id, entity_type, entity_id, org_id, rpm, tpm, concurrent_requests, updated_at
- `UsageLog` — id, timestamp, org_id, department_id, team_id, user_id, account_type, model, input_tokens, output_tokens, cost_usd, latency_ms, status_code, request_id, bedrock_account_id
- `BedrockPoolAccount` — id, account_id, role_arn, region, is_healthy, last_health_check, created_at
- `ModelAlias` — id, org_id, alias_name, bedrock_model_id
- `ModelPricing` — model_id, input_price_per_1k, output_price_per_1k, updated_at

### Pydantic Schemas
- `AuthExchangeRequest` — aws_access_key_id, aws_secret_access_key, aws_session_token
- `AuthExchangeResponse` — token, expires_at, user_id, org_id, team_id, department_id, account_type
- `TokenContext` — user_id, org_id, team_id, department_id, account_type, is_admin, expires_at
- `ErrorResponse` — error (code), message, details (optional dict)
- `BudgetConfigSchema` — entity_type, entity_id, period_type, budget_amount_usd, enforcement_mode
- `RateLimitConfigSchema` — entity_type, entity_id, rpm, tpm, concurrent_requests
- `UsageLogSchema` — all fields from UsageLog model
- `PoolAccountSchema` — account_id, role_arn, region, is_healthy

### Service Interfaces (Abstract Base Classes)
```python
class IAuthService(ABC):
    async def exchange_credentials(self, request: AuthExchangeRequest) -> AuthExchangeResponse
    async def validate_token(self, token: str) -> TokenContext
    async def revoke_token(self, token: str) -> None

class IProxyService(ABC):
    async def invoke(self, request: ProxyRequest, context: TokenContext) -> ProxyResponse
    async def invoke_stream(self, request: ProxyRequest, context: TokenContext) -> AsyncIterator[bytes]

class IBudgetService(ABC):
    async def check_budget(self, context: TokenContext) -> BudgetCheckResult
    async def record_usage(self, context: TokenContext, tokens_in: int, tokens_out: int, model: str) -> None
    async def get_budget_summary(self, entity_type: str, entity_id: str, org_id: str) -> BudgetSummary

class IRateLimitService(ABC):
    async def check_rate_limit(self, context: TokenContext) -> RateLimitCheckResult
    async def release_concurrent(self, context: TokenContext) -> None

class IPoolService(ABC):
    async def get_client(self) -> BedrockClientWrapper
    async def report_error(self, account_id: str) -> None
    async def get_pool_status(self) -> list[PoolAccountStatus]

class IUsageService(ABC):
    async def log_request(self, log: UsageLogSchema) -> None
    async def query_logs(self, filters: LogQueryFilters) -> list[UsageLogSchema]
    async def get_usage_summary(self, org_id: str, filters: UsageSummaryFilters) -> UsageSummary
```

### Shared Utilities
- `hash_token(token: str) -> str` — SHA-256 hash for token storage
- `generate_token() -> str` — generate `bg-` prefixed secure random token
- `calculate_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal` — cost from pricing table
- `get_settings() -> Settings` — Pydantic settings from env vars / config file

## Auth Component (`src/auth/`)

### AuthService (implements IAuthService)
- `exchange_credentials(request) -> AuthExchangeResponse` — STS validate → resolve tenant → generate token → store hash → return
- `validate_token(token) -> TokenContext` — hash token → lookup in DB → check expiry → return context
- `revoke_token(token) -> None` — mark token as revoked

### ServiceAccountService
- `create(org_id, name, iam_role_arn, department_id, team_id) -> ServiceAccount`
- `list(org_id) -> list[ServiceAccount]`
- `delete(org_id, service_account_id) -> None`

### TenantResolver
- `resolve(sts_identity: STSIdentity) -> TokenContext` — map account_id → org, role → dept/team, session → user

## Proxy Component (`src/proxy/`)

### ProxyService (implements IProxyService)
- `invoke(request, context) -> ProxyResponse` — translate format → get pool client → call Bedrock → translate response
- `invoke_stream(request, context) -> AsyncIterator` — same but streaming

### FormatTranslator
- `openai_to_bedrock(request: OpenAIChatRequest) -> BedrockInvokeRequest`
- `bedrock_to_openai(response: BedrockResponse) -> OpenAIChatResponse`
- `anthropic_to_bedrock(request: AnthropicMessagesRequest) -> BedrockInvokeRequest`
- `bedrock_to_anthropic(response: BedrockResponse) -> AnthropicMessagesResponse`

### ModelResolver
- `resolve(model_name: str, org_id: str) -> str` — resolve alias to Bedrock model ID
- `is_allowed(model_id: str, context: TokenContext) -> bool` — check model access

## Budget Component (`src/budget/`)

### BudgetService (implements IBudgetService)
- `check_budget(context) -> BudgetCheckResult` — check all levels (user → team → dept → org), return first hard-limit breach or soft-limit warnings
- `record_usage(context, tokens_in, tokens_out, model) -> None` — calculate cost, update budget_usage for all levels
- `get_budget_summary(entity_type, entity_id, org_id) -> BudgetSummary` — current spend vs budget with tree structure
- `set_budget(config: BudgetConfigSchema) -> None` — validate cascading constraints, save
- `validate_cascade(config) -> None` — ensure child budget ≤ parent budget

## Rate Limiting Component (`src/ratelimit/`)

### RateLimitService (implements IRateLimitService)
- `check_rate_limit(context) -> RateLimitCheckResult` — check all levels, return most restrictive breach
- `release_concurrent(context) -> None` — decrement concurrent counter after request completes

### RateLimitBackend (interface)
- `check_and_increment(key: str, limit: int, window_seconds: int) -> (allowed: bool, remaining: int, reset_at: datetime)`
- `increment_concurrent(key: str, limit: int) -> (allowed: bool, current: int)`
- `decrement_concurrent(key: str) -> None`

### InMemoryBackend (implements RateLimitBackend)
### RedisBackend (implements RateLimitBackend)

## Bedrock Pool Component (`src/pool/`)

### PoolService (implements IPoolService)
- `get_client() -> BedrockClientWrapper` — round-robin next healthy account, assume role, return client
- `report_error(account_id) -> None` — mark unhealthy, start cooldown timer
- `get_pool_status() -> list[PoolAccountStatus]` — health, request count, error count per account
- `health_check() -> None` — periodic check, restore healthy accounts

## Admin API Component (`src/admin/`)

### Routes
- `POST /admin/organizations` — create org (platform admin only)
- `GET /admin/organizations` — list orgs (platform admin) or own org (org admin)
- `PUT /admin/organizations/{org_id}` — update org config
- `POST /admin/organizations/{org_id}/service-accounts` — register service account
- `GET/PUT /admin/departments/{dept_id}/budgets` — dept budget management
- `GET/PUT /admin/teams/{team_id}/budgets` — team budget management
- `GET/PUT /admin/users/{user_id}/budgets` — user budget management
- `GET/PUT /admin/*/rate-limits` — rate limit management (same pattern)
- `GET /admin/logs` — query usage logs with filters
- `GET /admin/pool/status` — Bedrock pool health
- `GET /admin/usage/summary` — usage aggregation

### AccessControl
- `require_platform_admin(context: TokenContext) -> None` — raises 403 if not platform admin
- `require_org_admin(context: TokenContext, org_id: str) -> None` — raises 403 if not org admin for this org
- `require_dept_admin(context: TokenContext, dept_id: str) -> None` — raises 403 if not dept admin

## Usage Tracking Component (`src/usage/`)

### UsageService (implements IUsageService)
- `log_request(log) -> None` — insert to PostgreSQL
- `query_logs(filters) -> list[UsageLogSchema]` — filtered query with pagination
- `get_usage_summary(org_id, filters) -> UsageSummary` — aggregated by dept/team/user/model

### MetricsService
- `record_request(labels: dict) -> None` — increment Prometheus counters
- `record_latency(duration: float, labels: dict) -> None` — observe histogram
- `record_tokens(count: int, direction: str, labels: dict) -> None`
- `record_budget_utilization(ratio: float, labels: dict) -> None`
