# BedrockGateway - Requirements Document

## Executive Summary

BedrockGateway is a lightweight proxy service for Amazon Bedrock that provides centralized authentication, budget management, and rate limiting for enterprise teams. It integrates natively with AWS IAM Identity Center, eliminating the need for separate user management.

## Problem Statement

### Current Challenges

Organizations using Amazon Bedrock face several challenges:

1. **No Built-in Budget Controls**: Bedrock charges per token with no native budget limits per user/team
2. **No Rate Limiting**: No way to prevent a single user from consuming all capacity
3. **Credential Distribution**: Each user needs AWS credentials with Bedrock access
4. **Usage Visibility**: Hard to track who is using what and how much
5. **Cost Attribution**: Difficult to charge back costs to teams/projects

### Why Not LiteLLM?

LiteLLM is a great general-purpose proxy, but:
- Requires maintaining a separate user database
- SSO features require enterprise license for >5 users
- Overkill for Bedrock-only use cases
- No native IAM Identity Center integration

## Solution Overview

BedrockGateway is a purpose-built proxy for Bedrock with:
- Native IAM Identity Center integration (no separate user DB)
- Budget and rate limit enforcement at user and team levels
- Lightweight admin UI for configuration
- OpenAI-compatible API for broad client support

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              BedrockGateway                                  │
│                                                                              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐                   │
│  │   Admin UI   │    │  Proxy API   │    │  Auth API    │                   │
│  │  (React/Vue) │    │ (OpenAI fmt) │    │ (AWS creds)  │                   │
│  └──────────────┘    └──────────────┘    └──────────────┘                   │
│         │                   │                   │                            │
│         ▼                   ▼                   ▼                            │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                        Core Services                                 │    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌────────────┐  │    │
│  │  │   Budget    │  │    Rate     │  │   Usage     │  │   Auth     │  │    │
│  │  │   Manager   │  │   Limiter   │  │   Tracker   │  │  (STS)     │  │    │
│  │  └─────────────┘  └─────────────┘  └─────────────┘  └────────────┘  │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                    │                                         │
│                                    ▼                                         │
│                          ┌─────────────────┐                                 │
│                          │  Amazon Bedrock │                                 │
│                          └─────────────────┘                                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Functional Requirements

### FR1: Authentication via AWS Credentials

**FR1.1**: Users authenticate by exchanging AWS temporary credentials for a BedrockGateway API key

```
POST /auth/exchange
{
  "aws_access_key_id": "ASIA...",
  "aws_secret_access_key": "...",
  "aws_session_token": "..."
}

Response:
{
  "api_key": "bg-abc123...",
  "expires_at": "2024-01-15T14:00:00Z",
  "user_id": "john.doe@company.com",
  "team_id": "data-science"
}
```

**FR1.2**: Credentials are validated via STS GetCallerIdentity

**FR1.3**: User identity (email) is extracted from the IAM session name

**FR1.4**: Team membership is determined by IAM role (mapped in config)

**FR1.5**: API keys expire after configurable duration (default: 12 hours)

### FR2: Team and User Management (via Identity Center)

**FR2.1**: Teams are defined by IAM roles in Identity Center permission sets

**FR2.2**: No local user database - all identity comes from AWS

**FR2.3**: Admin UI shows users/teams synced from Identity Center (read-only)

**FR2.4**: Role-to-team mapping is configured in BedrockGateway:

```yaml
teams:
  data-science:
    iam_roles:
      - "arn:aws:iam::*:role/BedrockGateway-DataScience"
    models: ["anthropic.claude-*", "amazon.titan-*"]
    budget_monthly_usd: 500
    rate_limit_rpm: 100
    
  engineering:
    iam_roles:
      - "arn:aws:iam::*:role/BedrockGateway-Engineering"
    models: ["anthropic.claude-*"]
    budget_monthly_usd: 1000
    rate_limit_rpm: 200
```

### FR3: Budget Management

**FR3.1**: Budgets can be set at team level and user level

**FR3.2**: Budget periods: daily, weekly, monthly

**FR3.3**: When budget is exceeded:
- Soft limit: Log warning, continue allowing requests
- Hard limit: Reject requests with 429 status

**FR3.4**: Budget tracking includes:
- Input tokens
- Output tokens
- Total cost (calculated from Bedrock pricing)

**FR3.5**: Admin UI shows:
- Current spend vs budget (per user, per team)
- Spend trends over time
- Top users by spend

### FR4: Rate Limiting

**FR4.1**: Rate limits can be set at team level and user level

**FR4.2**: Rate limit types:
- Requests per minute (RPM)
- Tokens per minute (TPM)
- Concurrent requests

**FR4.3**: Rate limiting algorithm: Token bucket with configurable burst

**FR4.4**: When rate limited:
- Return 429 with Retry-After header
- Include remaining quota in response headers

### FR5: Proxy API (OpenAI-Compatible)

**FR5.1**: Support OpenAI chat completions format:

```
POST /v1/chat/completions
Authorization: Bearer bg-abc123...

{
  "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
  "messages": [{"role": "user", "content": "Hello"}]
}
```

**FR5.2**: Support streaming responses

**FR5.3**: Map OpenAI format to Bedrock Converse API

**FR5.4**: Support model aliases:
```yaml
model_aliases:
  "claude-3.5-sonnet": "anthropic.claude-3-5-sonnet-20241022-v2:0"
  "claude-3-opus": "anthropic.claude-3-opus-20240229-v1:0"
```

### FR6: Admin UI

**FR6.1**: Authentication via AWS credentials (same as API)

**FR6.2**: Admin role determined by IAM role mapping

**FR6.3**: Dashboard views:
- Overview: Total spend, active users, request volume
- Teams: List teams, budgets, usage
- Users: List users, their team, usage
- Logs: Recent requests with filters

**FR6.4**: Configuration:
- Team settings (budgets, rate limits, allowed models)
- User overrides (individual limits)
- Model aliases
- System settings

**FR6.5**: Lightweight UI framework (React or Vue, no heavy dependencies)

### FR7: Usage Tracking and Logging

**FR7.1**: Log every request with:
- Timestamp
- User ID
- Team ID
- Model
- Input/output tokens
- Latency
- Status code

**FR7.2**: Store logs in:
- Local database (SQLite/PostgreSQL) for querying
- Optional: CloudWatch Logs for long-term retention

**FR7.3**: Metrics exposed via:
- `/metrics` endpoint (Prometheus format)
- CloudWatch custom metrics (optional)

## Non-Functional Requirements

### NFR1: Performance

- Proxy latency overhead: <50ms p99
- Support 1000+ concurrent connections
- Handle 10,000+ RPM

### NFR2: Availability

- Stateless design (can run multiple instances)
- Health check endpoint for load balancer
- Graceful degradation if database unavailable

### NFR3: Security

- AWS credentials never stored (used once for STS validation)
- API keys stored hashed
- All traffic over HTTPS
- Admin actions logged

### NFR4: Deployment

- Container image (Docker)
- Helm chart for Kubernetes
- CloudFormation/CDK for AWS deployment
- Environment variable configuration

### NFR5: Observability

- Structured logging (JSON)
- Distributed tracing (X-Ray compatible)
- Prometheus metrics
- Health/readiness endpoints

## Technical Architecture

### Components

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              BedrockGateway                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                           API Layer                                  │    │
│  │                                                                      │    │
│  │  /auth/exchange     - Exchange AWS creds for API key                │    │
│  │  /v1/chat/completions - OpenAI-compatible proxy                     │    │
│  │  /v1/models         - List available models                         │    │
│  │  /admin/*           - Admin API endpoints                           │    │
│  │  /metrics           - Prometheus metrics                            │    │
│  │  /health            - Health check                                  │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                    │                                         │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                        Middleware Layer                              │    │
│  │                                                                      │    │
│  │  AuthMiddleware     - Validate API key, extract user/team           │    │
│  │  RateLimitMiddleware - Check and enforce rate limits                │    │
│  │  BudgetMiddleware   - Check budget before request                   │    │
│  │  LoggingMiddleware  - Log request/response                          │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                    │                                         │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                        Service Layer                                 │    │
│  │                                                                      │    │
│  │  AuthService        - STS validation, key generation                │    │
│  │  BedrockService     - Bedrock API calls                             │    │
│  │  BudgetService      - Budget tracking and enforcement               │    │
│  │  RateLimitService   - Rate limit tracking (Redis/in-memory)         │    │
│  │  UsageService       - Usage logging and aggregation                 │    │
│  │  ConfigService      - Team/model configuration                      │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                    │                                         │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                        Data Layer                                    │    │
│  │                                                                      │    │
│  │  PostgreSQL/SQLite  - Config, usage logs, budget tracking           │    │
│  │  Redis (optional)   - Rate limiting, session cache                  │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                          ┌─────────────────┐
                          │  Amazon Bedrock │
                          │  (Converse API) │
                          └─────────────────┘
```

### Technology Stack

| Component | Technology | Rationale |
|-----------|------------|-----------|
| Backend | Python (FastAPI) or Go | Fast, async, good AWS SDK support |
| Database | PostgreSQL (prod) / SQLite (dev) | Reliable, good for time-series queries |
| Cache | Redis (optional) | Rate limiting, distributed deployments |
| Admin UI | React + Tailwind | Lightweight, modern |
| Container | Docker | Standard deployment |
| IaC | CDK or Terraform | AWS-native deployment |

### Data Model

```sql
-- API Keys (hashed)
CREATE TABLE api_keys (
    id UUID PRIMARY KEY,
    key_hash VARCHAR(64) NOT NULL,
    user_id VARCHAR(255) NOT NULL,
    team_id VARCHAR(255) NOT NULL,
    aws_account_id VARCHAR(12) NOT NULL,
    aws_role_arn VARCHAR(255) NOT NULL,
    created_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    revoked_at TIMESTAMP
);

-- Usage Logs
CREATE TABLE usage_logs (
    id UUID PRIMARY KEY,
    timestamp TIMESTAMP NOT NULL,
    user_id VARCHAR(255) NOT NULL,
    team_id VARCHAR(255) NOT NULL,
    model VARCHAR(255) NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd DECIMAL(10, 6) NOT NULL,
    latency_ms INTEGER NOT NULL,
    status_code INTEGER NOT NULL,
    request_id VARCHAR(255)
);

-- Budget Tracking (aggregated)
CREATE TABLE budget_usage (
    id UUID PRIMARY KEY,
    entity_type VARCHAR(10) NOT NULL, -- 'user' or 'team'
    entity_id VARCHAR(255) NOT NULL,
    period_start DATE NOT NULL,
    period_type VARCHAR(10) NOT NULL, -- 'daily', 'weekly', 'monthly'
    total_cost_usd DECIMAL(10, 2) NOT NULL,
    total_tokens INTEGER NOT NULL,
    request_count INTEGER NOT NULL,
    UNIQUE(entity_type, entity_id, period_start, period_type)
);

-- Team Configuration
CREATE TABLE team_config (
    team_id VARCHAR(255) PRIMARY KEY,
    budget_monthly_usd DECIMAL(10, 2),
    budget_daily_usd DECIMAL(10, 2),
    rate_limit_rpm INTEGER,
    rate_limit_tpm INTEGER,
    allowed_models JSONB,
    settings JSONB,
    updated_at TIMESTAMP NOT NULL
);

-- User Overrides (optional per-user limits)
CREATE TABLE user_overrides (
    user_id VARCHAR(255) PRIMARY KEY,
    budget_monthly_usd DECIMAL(10, 2),
    budget_daily_usd DECIMAL(10, 2),
    rate_limit_rpm INTEGER,
    rate_limit_tpm INTEGER,
    allowed_models JSONB,
    updated_at TIMESTAMP NOT NULL
);
```

## User Flows

### Flow 1: User Authentication

```
┌─────────┐          ┌─────────────────┐          ┌─────────┐
│  User   │          │ BedrockGateway  │          │   AWS   │
└────┬────┘          └────────┬────────┘          └────┬────┘
     │                        │                        │
     │  aws sso login         │                        │
     │───────────────────────────────────────────────▶│
     │                        │                        │
     │  AWS temp credentials  │                        │
     │◀───────────────────────────────────────────────│
     │                        │                        │
     │  POST /auth/exchange   │                        │
     │  {aws_creds}           │                        │
     │───────────────────────▶│                        │
     │                        │                        │
     │                        │  sts:GetCallerIdentity │
     │                        │───────────────────────▶│
     │                        │                        │
     │                        │  {Arn, Account, UserId}│
     │                        │◀───────────────────────│
     │                        │                        │
     │                        │  Validate account      │
     │                        │  Map role → team       │
     │                        │  Generate API key      │
     │                        │                        │
     │  {api_key, expires_at} │                        │
     │◀───────────────────────│                        │
     │                        │                        │
```

### Flow 2: API Request with Budget/Rate Check

```
┌─────────┐          ┌─────────────────┐          ┌─────────┐
│  User   │          │ BedrockGateway  │          │ Bedrock │
└────┬────┘          └────────┬────────┘          └────┬────┘
     │                        │                        │
     │  POST /v1/chat/completions                      │
     │  Authorization: Bearer bg-...                   │
     │───────────────────────▶│                        │
     │                        │                        │
     │                        │  1. Validate API key   │
     │                        │  2. Check rate limit   │
     │                        │  3. Check budget       │
     │                        │                        │
     │                        │  (if any check fails)  │
     │  429 Too Many Requests │                        │
     │◀ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─│                        │
     │                        │                        │
     │                        │  (if all checks pass)  │
     │                        │  Converse API          │
     │                        │───────────────────────▶│
     │                        │                        │
     │                        │  Response + tokens     │
     │                        │◀───────────────────────│
     │                        │                        │
     │                        │  4. Log usage          │
     │                        │  5. Update budget      │
     │                        │                        │
     │  Response              │                        │
     │◀───────────────────────│                        │
     │                        │                        │
```

## Configuration

### Environment Variables

```bash
# Required
AWS_REGION=us-east-1
DATABASE_URL=postgresql://user:pass@host:5432/bedrockgw

# Optional
REDIS_URL=redis://localhost:6379
LOG_LEVEL=INFO
API_KEY_DURATION_HOURS=12
ALLOWED_AWS_ACCOUNTS=123456789012,987654321098
```

### Configuration File (config.yaml)

```yaml
server:
  port: 8080
  host: 0.0.0.0

auth:
  allowed_accounts:
    - "123456789012"
  api_key_duration_hours: 12
  admin_roles:
    - "arn:aws:iam::*:role/BedrockGateway-Admin"

teams:
  data-science:
    iam_roles:
      - "arn:aws:iam::*:role/BedrockGateway-DataScience"
    allowed_models:
      - "anthropic.claude-*"
      - "amazon.titan-*"
    budget:
      monthly_usd: 500
      daily_usd: 50
      enforcement: hard  # or 'soft'
    rate_limits:
      requests_per_minute: 100
      tokens_per_minute: 100000
      concurrent_requests: 10

  engineering:
    iam_roles:
      - "arn:aws:iam::*:role/BedrockGateway-Engineering"
    allowed_models:
      - "anthropic.claude-*"
    budget:
      monthly_usd: 1000
      enforcement: soft
    rate_limits:
      requests_per_minute: 200

models:
  aliases:
    "claude-3.5-sonnet": "anthropic.claude-3-5-sonnet-20241022-v2:0"
    "claude-3-opus": "anthropic.claude-3-opus-20240229-v1:0"
  
  pricing:  # USD per 1K tokens
    "anthropic.claude-3-5-sonnet-20241022-v2:0":
      input: 0.003
      output: 0.015
    "anthropic.claude-3-opus-20240229-v1:0":
      input: 0.015
      output: 0.075

logging:
  format: json
  level: INFO
  cloudwatch:
    enabled: true
    log_group: /bedrockgateway/requests
```

## Deployment Options

### Option 1: ECS Fargate

```
┌─────────────────────────────────────────────────────────────┐
│                         VPC                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │                   Private Subnet                     │    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │    │
│  │  │   ECS Task  │  │   ECS Task  │  │     RDS     │  │    │
│  │  │ (Gateway)   │  │ (Gateway)   │  │ (PostgreSQL)│  │    │
│  │  └─────────────┘  └─────────────┘  └─────────────┘  │    │
│  └─────────────────────────────────────────────────────┘    │
│                           │                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │                   Public Subnet                      │    │
│  │  ┌─────────────────────────────────────────────┐    │    │
│  │  │              ALB (HTTPS)                     │    │    │
│  │  └─────────────────────────────────────────────┘    │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

### Option 2: Lambda + API Gateway

- Lower cost for low traffic
- No server management
- Cold start latency consideration

### Option 3: EKS

- For organizations already using Kubernetes
- Helm chart provided

## Implementation Phases

### Phase 1: Core Proxy (MVP)
- Auth endpoint (AWS credential exchange)
- OpenAI-compatible proxy to Bedrock
- Basic rate limiting (in-memory)
- Usage logging to database
- Health endpoints

### Phase 2: Budget Management
- Budget tracking per user/team
- Budget enforcement (soft/hard limits)
- Budget alerts

### Phase 3: Admin UI
- Dashboard with usage overview
- Team/user management
- Configuration UI
- Log viewer

### Phase 4: Advanced Features
- Redis for distributed rate limiting
- CloudWatch integration
- Prometheus metrics
- Model aliases and routing

## Success Criteria

1. **Authentication**: Users can exchange AWS SSO credentials for API keys
2. **Proxy**: OpenAI-compatible API works with Claude Code and other clients
3. **Rate Limiting**: Requests are rate limited per user/team configuration
4. **Budget Enforcement**: Requests are rejected when budget exceeded (hard limit)
5. **Visibility**: Admins can see usage per user/team in UI
6. **Performance**: <50ms overhead on proxied requests
7. **Reliability**: 99.9% uptime with proper deployment

## Comparison: BedrockGateway vs LiteLLM

| Feature | BedrockGateway | LiteLLM |
|---------|----------------|---------|
| Focus | Bedrock-only | Multi-provider |
| User Management | IAM Identity Center | Built-in DB |
| SSO | Native (free) | Enterprise license |
| Complexity | Simple | Feature-rich |
| Budget Management | Built-in | Built-in |
| Rate Limiting | Built-in | Built-in |
| Admin UI | Lightweight | Full-featured |
| Deployment | Container/Lambda | Container |

## Open Questions

1. **Multi-region**: Should BedrockGateway support routing to multiple Bedrock regions?
2. **Caching**: Should responses be cached for identical requests?
3. **Request Logging**: How long to retain detailed request logs?
4. **Alerting**: Integration with SNS/PagerDuty for budget alerts?
5. **Audit**: CloudTrail integration for compliance?

## References

- [Amazon Bedrock Converse API](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html)
- [AWS IAM Identity Center](https://docs.aws.amazon.com/singlesignon/latest/userguide/what-is.html)
- [OpenAI API Reference](https://platform.openai.com/docs/api-reference)
- [LiteLLM Documentation](https://docs.litellm.ai/)
