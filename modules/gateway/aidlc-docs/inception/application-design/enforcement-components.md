# Enforcement Components Design - Issue #131

## Overview

This document describes the new components required to implement cascading budget management and rate limiting enforcement in the proxy path.

## Component Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                            FastAPI Application                                │
│                                                                               │
│  ┌─────────────────────────────────────────────────────────────────────────┐ │
│  │                         Middleware Chain                                 │ │
│  │                                                                          │ │
│  │  Request → [AuthMiddleware] → [RateLimitMiddleware] → [BudgetMiddleware] │ │
│  │                                        ↓                    ↓            │ │
│  │                               Check rate limits    Check budget limits   │ │
│  │                               (429 if exceeded)    (429 if exceeded)     │ │
│  │                                        │                    │            │ │
│  │                                        └────────┬───────────┘            │ │
│  │                                                 ↓                        │ │
│  │                                         [ProxyRoutes]                    │ │
│  │                                                 │                        │ │
│  │                                                 ↓                        │ │
│  │                                         [ProxyService]                   │ │
│  │                                                 │                        │ │
│  │                                                 ↓                        │ │
│  │                                       [BedrockClient]                    │ │
│  │                                                 │                        │ │
│  │                                                 ↓                        │ │
│  │  Response ← [ResponseHeaders] ← [UsageRecorder] ← [BedrockResponse]     │ │
│  └─────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                           Supporting Services                                │
│                                                                              │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐   │
│  │ BudgetEnforcement│  │  RateLimitEnf.   │  │    PricingService        │   │
│  │    Service       │  │    Service       │  │                          │   │
│  │                  │  │                  │  │  - Model pricing table   │   │
│  │ - Hierarchy check│  │ - Token buckets  │  │  - Cost calculation      │   │
│  │ - Cost estimation│  │ - Concurrent     │  │  - Token estimation      │   │
│  │ - Usage recording│  │   tracking       │  │                          │   │
│  └────────┬─────────┘  └────────┬─────────┘  └──────────────────────────┘   │
│           │                     │                                            │
│           └──────────┬──────────┘                                            │
│                      ↓                                                       │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │                         Data Layer                                     │  │
│  │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────┐    │  │
│  │  │  PostgreSQL     │  │     Redis       │  │   In-Memory Cache   │    │  │
│  │  │  (persistent)   │  │  (rate limits)  │  │  (config cache)     │    │  │
│  │  │                 │  │                 │  │                     │    │  │
│  │  │ - BudgetConfig  │  │ - Token buckets │  │ - Budget configs    │    │  │
│  │  │ - BudgetUsage   │  │ - Concurrent    │  │ - Rate limit configs│    │  │
│  │  │ - RateLimitCfg  │  │   counters      │  │                     │    │  │
│  │  └─────────────────┘  └─────────────────┘  └─────────────────────┘    │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## New Component Definitions

### 1. BudgetEnforcementMiddleware

**Location**: `src/budget/enforcement_middleware.py`

**Purpose**: Intercept proxy requests to check budget constraints before forwarding to Bedrock.

**Responsibilities**:
- Extract TokenContext from request state (set by AuthMiddleware)
- Estimate request cost from request body
- Call BudgetEnforcementService to check hierarchy
- Return 429 if hard limit exceeded
- Add warning headers if soft limit exceeded
- Store enforcement result in request state for post-response recording

**Interface**:
```python
class BudgetEnforcementMiddleware:
    def __init__(self, budget_service: BudgetEnforcementService):
        self.budget_service = budget_service

    async def __call__(
        self,
        request: Request,
        call_next: Callable
    ) -> Response:
        """
        Check budget before request, record usage after response.
        """
        pass
```

---

### 2. RateLimitEnforcementMiddleware

**Location**: `src/ratelimit/enforcement_middleware.py`

**Purpose**: Intercept proxy requests to check rate limit constraints.

**Responsibilities**:
- Extract TokenContext from request state
- Check RPM limits at all hierarchy levels
- Check concurrent request limits
- Return 429 with Retry-After if limit exceeded
- Add rate limit headers to response
- Track concurrent requests (increment/decrement)

**Interface**:
```python
class RateLimitEnforcementMiddleware:
    def __init__(self, ratelimit_service: RateLimitService):
        self.ratelimit_service = ratelimit_service

    async def __call__(
        self,
        request: Request,
        call_next: Callable
    ) -> Response:
        """
        Check rate limits before request, update headers after.
        """
        pass
```

---

### 3. BudgetEnforcementService

**Location**: `src/budget/enforcement_service.py`

**Purpose**: Core business logic for cascading budget enforcement.

**Responsibilities**:
- Traverse entity hierarchy (user → team → dept → org)
- Check budget status at each level
- Aggregate enforcement decisions
- Calculate estimated and actual costs
- Record usage after request completion

**Interface**:
```python
class BudgetEnforcementService:
    def __init__(
        self,
        budget_service: BudgetService,
        pricing_service: PricingService,
        db_session: AsyncSession = None
    ):
        pass

    async def check_budget_hierarchy(
        self,
        context: TokenContext,
        estimated_cost: Decimal
    ) -> EnforcementResult:
        """
        Check budgets at all hierarchy levels.
        Returns EnforcementResult with allowed/blocked status.
        """
        pass

    async def record_usage(
        self,
        context: TokenContext,
        input_tokens: int,
        output_tokens: int,
        model_id: str
    ) -> None:
        """
        Record usage to all hierarchy levels after request completes.
        """
        pass

    def get_budget_headers(
        self,
        context: TokenContext
    ) -> dict[str, str]:
        """
        Generate X-Budget-* headers for response.
        """
        pass
```

---

### 4. PricingService

**Location**: `src/budget/pricing.py`

**Purpose**: Centralized cost calculation for Bedrock models.

**Responsibilities**:
- Maintain static pricing table for all supported models
- Calculate cost from token counts
- Estimate input tokens from request body
- Provide default pricing for unknown models

**Pricing Table**:
```python
# Pricing per 1000 tokens (USD)
MODEL_PRICING = {
    # Claude 3.5 models
    "anthropic.claude-3-5-sonnet-20241022-v2:0": {"input": 0.003, "output": 0.015},
    "anthropic.claude-3-5-haiku-20241022-v1:0": {"input": 0.0008, "output": 0.004},

    # Claude 3 models
    "anthropic.claude-3-opus-20240229-v1:0": {"input": 0.015, "output": 0.075},
    "anthropic.claude-3-sonnet-20240229-v1:0": {"input": 0.003, "output": 0.015},
    "anthropic.claude-3-haiku-20240307-v1:0": {"input": 0.00025, "output": 0.00125},

    # Amazon Titan models
    "amazon.titan-text-express-v1": {"input": 0.0002, "output": 0.0006},
    "amazon.titan-text-lite-v1": {"input": 0.00015, "output": 0.0002},

    # Default fallback
    "default": {"input": 0.001, "output": 0.003}
}
```

**Interface**:
```python
class PricingService:
    def calculate_cost(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int
    ) -> Decimal:
        """Calculate total cost for a request."""
        pass

    def estimate_input_tokens(
        self,
        request_body: dict
    ) -> int:
        """Estimate input tokens from request body size."""
        pass

    def estimate_output_tokens(
        self,
        max_tokens: int | None
    ) -> int:
        """Estimate output tokens for pre-request budget check."""
        pass

    def get_model_pricing(
        self,
        model_id: str
    ) -> dict[str, Decimal]:
        """Get pricing info for a specific model."""
        pass
```

---

### 5. ResponseHeadersService

**Location**: `src/shared/headers.py`

**Purpose**: Generate and inject response headers for rate limits and budgets.

**Headers Specification**:

| Header | Description | Example |
|--------|-------------|---------|
| `X-Budget-Limit` | Budget limit in USD | `500.00` |
| `X-Budget-Remaining` | Remaining budget in USD | `125.50` |
| `X-Budget-Reset` | Period reset date (ISO 8601) | `2024-02-01` |
| `X-Budget-Warning` | Soft limit warning message | `Team budget at 90%` |
| `X-RateLimit-Limit` | Rate limit per window | `60` |
| `X-RateLimit-Remaining` | Remaining requests | `45` |
| `X-RateLimit-Reset` | Unix timestamp of reset | `1706789400` |
| `Retry-After` | Seconds to wait (429 only) | `30` |

**Interface**:
```python
class ResponseHeadersService:
    @staticmethod
    def budget_headers(
        budget_status: BudgetStatusResponse
    ) -> dict[str, str]:
        """Generate X-Budget-* headers."""
        pass

    @staticmethod
    def rate_limit_headers(
        rate_limit_status: RateLimitStatusResponse
    ) -> dict[str, str]:
        """Generate X-RateLimit-* headers."""
        pass

    @staticmethod
    def warning_headers(
        warnings: list[str]
    ) -> dict[str, str]:
        """Generate X-*-Warning headers."""
        pass

    @staticmethod
    def inject_headers(
        response: Response,
        headers: dict[str, str]
    ) -> Response:
        """Add headers to response object."""
        pass
```

---

### 6. EnforcementResult (Data Class)

**Location**: `src/shared/schemas/enforcement.py`

**Purpose**: Encapsulate enforcement check results.

```python
@dataclass
class EnforcementResult:
    allowed: bool
    blocked_reason: str | None = None
    exceeded_entity_type: str | None = None
    exceeded_entity_id: str | None = None
    budget_amount_usd: Decimal | None = None
    current_spend_usd: Decimal | None = None
    enforcement_mode: str | None = None  # "soft" or "hard"
    warnings: list[str] = field(default_factory=list)
    retry_after_seconds: int | None = None
```

---

## Data Flow: Request Lifecycle

```
1. Request Arrives
   │
   ├─→ AuthMiddleware (existing)
   │   ├── Validate JWT
   │   ├── Extract TokenContext (user_id, team_id, dept_id, org_id)
   │   └── Attach to request.state.token_context
   │
   ├─→ RateLimitEnforcementMiddleware (NEW)
   │   ├── Get TokenContext from request.state
   │   ├── Check RPM at user → team → dept → org
   │   ├── Check concurrent limits
   │   ├── IF exceeded: Return 429 with headers
   │   ├── ELSE: Increment concurrent count
   │   └── Continue to next middleware
   │
   ├─→ BudgetEnforcementMiddleware (NEW)
   │   ├── Get TokenContext from request.state
   │   ├── Estimate cost from request body
   │   ├── Check budgets at user → team → dept → org
   │   ├── IF hard limit exceeded: Return 429 with error
   │   ├── IF soft limit exceeded: Add warning headers
   │   ├── Store enforcement context in request.state
   │   └── Continue to next handler
   │
   ├─→ ProxyRoutes / ProxyService (existing, enhanced)
   │   ├── Forward request to Bedrock
   │   ├── Stream response to client
   │   └── Count tokens during streaming
   │
   └─→ Response Processing
       ├── Extract actual token counts
       ├── Record usage to database (async)
       ├── Decrement concurrent count
       ├── Add response headers
       └── Return response to client
```

---

## Error Handling

### Rate Limit Exceeded (429)
```json
{
  "error": "rate_limited",
  "message": "Rate limit exceeded for user user-123",
  "details": {
    "limit_type": "rpm",
    "limit": 60,
    "remaining": 0,
    "reset_seconds": 30,
    "exceeded_at": "user"
  }
}
```

### Budget Exceeded (429)
```json
{
  "error": "budget_exceeded",
  "message": "Budget exceeded for team team-456",
  "details": {
    "entity_type": "team",
    "entity_id": "team-456",
    "budget_usd": 2000.00,
    "spent_usd": 2015.50,
    "period_type": "monthly",
    "reset_date": "2024-02-01"
  }
}
```

---

## Configuration

### Environment Variables
```bash
# Budget enforcement
BUDGET_ENFORCEMENT_ENABLED=true
BUDGET_ESTIMATE_OUTPUT_TOKENS=500  # Default output token estimate

# Rate limiting
RATELIMIT_ENFORCEMENT_ENABLED=true
RATELIMIT_BACKEND=memory  # or "redis"
RATELIMIT_REDIS_URL=redis://localhost:6379/0

# Fail-open behavior
ENFORCEMENT_FAIL_OPEN=true  # Allow requests if enforcement check fails
```

---

## Dependencies

```
BudgetEnforcementMiddleware
    └── BudgetEnforcementService
        ├── BudgetService (existing)
        ├── PricingService (new)
        └── Database (PostgreSQL)

RateLimitEnforcementMiddleware
    └── RateLimitService (existing, enhanced)
        └── Backend (In-Memory or Redis)

ProxyService (existing)
    └── StreamTokenCounter (enhanced StreamHandler)
```

---

## File Structure

```
src/
├── budget/
│   ├── __init__.py
│   ├── config.py           # (existing)
│   ├── routes.py           # (existing)
│   ├── service.py          # (existing)
│   ├── utils.py            # (existing)
│   ├── enforcement_service.py  # NEW - cascading logic
│   ├── enforcement_middleware.py  # NEW - middleware
│   └── pricing.py          # NEW - cost calculation
├── ratelimit/
│   ├── __init__.py
│   ├── config.py           # (existing)
│   ├── routes.py           # (existing)
│   ├── service.py          # (existing)
│   ├── backend.py          # (existing)
│   ├── token_bucket.py     # (existing)
│   ├── models.py           # (existing)
│   ├── backends/
│   │   ├── in_memory.py    # (existing)
│   │   └── redis.py        # (existing)
│   └── enforcement_middleware.py  # NEW - middleware
├── proxy/
│   ├── routes.py           # MODIFIED - wire middleware
│   ├── service.py          # MODIFIED - token counting
│   └── stream_handler.py   # MODIFIED - token counting
└── shared/
    ├── schemas/
    │   └── enforcement.py  # NEW - EnforcementResult
    └── headers.py          # NEW - response headers
```
