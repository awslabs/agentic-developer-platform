# Cascading Budget Management and Rate Limiting

This document describes the cascading budget management and rate limiting system implemented in BedrockGateway. This feature enforces cost controls and usage limits at every level of the organizational hierarchy.

## Overview

BedrockGateway implements a cascading enforcement model that checks constraints at multiple levels:

```
Organization
└── Department
    └── Team
        └── User / Service Account
```

When a request is made, the system checks budgets and rate limits at ALL levels. If any level exceeds its hard limit, the request is blocked. Soft limits generate warnings but allow the request to proceed.

## Budget Management (FR3)

### How Cascading Budgets Work

Budgets use an **Independent Limits with Aggregated Tracking** model:

1. Each hierarchy level can have its own independent budget
2. When a user makes a request, the cost is attributed to ALL levels simultaneously
3. Usage accumulates at each level independently
4. Enforcement is checked at ALL levels

**Example:**
```
Org: $10,000/month budget (hard limit)
├── Dept-Eng: $5,000/month (soft limit)
│   ├── Team-AI: $2,000/month (hard limit)
│   │   ├── User-Alice: $500/month (soft limit)
│   │   └── User-Bob: $500/month (hard limit)
```

When Alice makes a $10 request:
- Alice's usage: +$10 (checked against $500 limit)
- Team-AI usage: +$10 (checked against $2,000 limit)
- Dept-Eng usage: +$10 (checked against $5,000 limit)
- Org usage: +$10 (checked against $10,000 limit)

### Enforcement Modes

| Mode | Behavior |
|------|----------|
| **SOFT** | Log warning, add `X-Budget-Warning` header, allow request |
| **HARD** | Reject request with HTTP 429, include error details |

### Budget Periods

Budgets can be configured with different reset periods:

| Period | Reset Time |
|--------|------------|
| `daily` | Midnight UTC |
| `weekly` | Sunday midnight UTC |
| `monthly` | 1st of month, midnight UTC |

### Cost Calculation

Costs are calculated using a static pricing table with model-specific rates:

```python
cost = (input_tokens / 1000 * input_price) + (output_tokens / 1000 * output_price)
```

Pricing is based on AWS Bedrock published rates. See `src/budget/pricing.py` for the full pricing table.

### Pre-Request vs Post-Request

1. **Pre-request**: Estimate input tokens from request body, estimate output tokens from `max_tokens` parameter
2. **Execute**: Proxy the request to Bedrock
3. **Post-request**: Calculate actual cost from response tokens, record usage

If a streaming request starts under budget but ends over budget, the request completes (grace period). The over-budget amount is recorded, and the next request will be blocked.

## Rate Limiting (FR4)

### Rate Limit Types

| Type | Description |
|------|-------------|
| **RPM** | Requests per minute |
| **TPM** | Tokens per minute |
| **Concurrent** | Maximum simultaneous requests |

### Token Bucket Algorithm

Rate limits use a token bucket algorithm:

- **Capacity**: Maximum burst size (configurable via `burst_size`)
- **Refill Rate**: Tokens added per second (derived from limit/60)

Example: 60 RPM = capacity of 60, refill rate of 1 token/second

### Hierarchical Enforcement

Rate limits are checked at all hierarchy levels. The most restrictive limit wins:

```
If user is at 55 RPM but team is at 200 RPM (limit):
  → Request blocked at team level
  → Error message indicates "team" limit exceeded
```

### State Storage

| Backend | Use Case |
|---------|----------|
| **In-Memory** | Single-instance deployment, development, testing |
| **Redis** | Multi-instance deployment, production HA |

Configure via environment variables:
```bash
RATELIMIT_BACKEND=redis
RATELIMIT_REDIS_URL=redis://elasticache:6379/0
```

## API Reference

### Error Responses

#### 429 Budget Exceeded
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

#### 429 Rate Limited
```json
{
  "error": "rate_limited",
  "message": "Rate limit exceeded for user user-123",
  "details": {
    "limit_type": "rpm",
    "limit": 60,
    "remaining": 0,
    "reset_seconds": 30
  }
}
```

### Response Headers

#### Budget Headers
| Header | Description | Example |
|--------|-------------|---------|
| `X-Budget-Limit` | Budget limit in USD | `500.00` |
| `X-Budget-Remaining` | Remaining budget | `125.50` |
| `X-Budget-Reset` | Period reset date | `2024-02-01` |
| `X-Budget-Warning` | Soft limit warning | `Team budget at 90%` |

#### Rate Limit Headers
| Header | Description | Example |
|--------|-------------|---------|
| `X-RateLimit-Limit` | Rate limit | `60` |
| `X-RateLimit-Remaining` | Remaining requests | `45` |
| `X-RateLimit-Reset` | Seconds until reset | `30` |
| `Retry-After` | Seconds to wait (429 only) | `30` |

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

### Admin API Endpoints

#### Configure Budget
```bash
# Set user budget
curl -X PUT /api/budgets \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "entity_type": "user",
    "entity_id": "user-123",
    "period_type": "monthly",
    "budget_amount_usd": 500.00,
    "enforcement_mode": "hard"
  }'
```

#### Configure Rate Limit
```bash
# Set user rate limit
curl -X PUT /api/ratelimits/user/user-123 \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "rpm": 60,
    "tpm": 100000,
    "concurrent_requests": 5
  }'
```

## Troubleshooting

### Common 429 Errors

#### "Budget exceeded for organization"
- Organization-level monthly budget is exhausted
- Contact platform admin to increase budget or wait for period reset

#### "Budget exceeded for team"
- Team budget is exhausted but organization has remaining budget
- Contact team admin to redistribute budget

#### "Rate limit exceeded (rpm)"
- Too many requests per minute
- Wait for `Retry-After` seconds, or slow down request rate

#### "Rate limit exceeded (concurrent)"
- Too many simultaneous requests
- Wait for existing requests to complete

### Debugging

Enable debug logging to see enforcement decisions:

```bash
BG_LOG_LEVEL=DEBUG
```

Log output includes:
- Budget check results at each hierarchy level
- Rate limit bucket state
- Usage recording confirmation

## Architecture

### Middleware Order

```
Request → Auth → RateLimit → Budget → Proxy → Response
                    ↓            ↓
              Check limits   Check budget
                    ↓            ↓
              429 if exceeded  429 if exceeded
                                 ↓
                           Record usage (post-response)
```

### File Structure

```
src/
├── budget/
│   ├── enforcement_service.py  # Cascading budget logic
│   ├── enforcement_middleware.py  # FastAPI middleware
│   ├── pricing.py  # Model pricing table
│   └── ...
├── ratelimit/
│   ├── service.py  # Rate limit service
│   ├── enforcement_middleware.py  # FastAPI middleware
│   └── backends/
│       ├── in_memory.py
│       └── redis.py
└── shared/
    └── headers.py  # Response header utilities
```

## Security Considerations

1. **Multi-tenant isolation**: All budget/rate limit checks are scoped by `org_id`
2. **Fail-open**: On transient errors, requests are allowed (configurable)
3. **Admin bypass**: Platform admins can temporarily override limits (future feature)
4. **Audit logging**: All enforcement decisions are logged with full context
