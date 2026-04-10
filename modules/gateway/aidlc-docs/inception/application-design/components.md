# Application Components

## Component Architecture Overview

```
+------------------------------------------------------------------+
|                        API Layer                                  |
|  +-------------+  +-------------+  +-------------+  +----------+ |
|  | Auth API    |  | Proxy API   |  | Admin API   |  | Metrics  | |
|  | /auth/*     |  | /v1/*       |  | /admin/*    |  | /metrics | |
|  |             |  | /bedrock/*  |  |             |  | /health  | |
|  +------+------+  +------+------+  +------+------+  +----------+ |
|         |                |                |                       |
+---------+----------------+----------------+-----------------------+
          |                |                |
+---------v----------------v----------------v-----------------------+
|                     Middleware Layer                               |
|  +----------------+  +----------------+  +---------------------+  |
|  | Auth           |  | Rate Limit     |  | Budget              |  |
|  | Middleware      |  | Middleware     |  | Middleware           |  |
|  +----------------+  +----------------+  +---------------------+  |
|  +----------------+  +----------------+                           |
|  | Logging        |  | Tenant Context |                           |
|  | Middleware      |  | Middleware     |                           |
|  +----------------+  +----------------+                           |
+-------------------------------------------------------------------+
          |                |                |
+---------v----------------v----------------v-----------------------+
|                      Service Layer                                |
|  +-------------+  +-------------+  +-------------+  +----------+ |
|  | AuthService |  | ProxyService|  | BudgetSvc   |  | RateLimit| |
|  |             |  |             |  |             |  | Service  | |
|  +-------------+  +-------------+  +-------------+  +----------+ |
|  +-------------+  +-------------+  +-------------+  +----------+ |
|  | UsageService|  | PoolService |  | ConfigSvc   |  | IdentSvc | |
|  |             |  |             |  |             |  |          | |
|  +-------------+  +-------------+  +-------------+  +----------+ |
+-------------------------------------------------------------------+
          |                |                |
+---------v----------------v----------------v-----------------------+
|                      Data Layer                                   |
|  +---------------------------+  +-------------------------------+ |
|  | PostgreSQL (Repository)   |  | Redis (optional)              | |
|  | - OrgRepository           |  | - RateLimitStore              | |
|  | - UserRepository          |  | - SessionCache                | |
|  | - TokenRepository         |  +-------------------------------+ |
|  | - BudgetRepository        |                                    |
|  | - UsageRepository         |  +-------------------------------+ |
|  | - ConfigRepository        |  | AWS Clients                   | |
|  +---------------------------+  | - STSClient                   | |
|                                 | - BedrockClient               | |
|                                 +-------------------------------+ |
+-------------------------------------------------------------------+
```

## Components

### 1. Shared Foundation (`src/shared/`)
**Purpose**: Common code that ALL units depend on. Committed to main before agents start.

**Responsibilities**:
- Database models (SQLAlchemy ORM models)
- Pydantic schemas (request/response models)
- Interface definitions (abstract base classes for services)
- Configuration schemas and loading
- Common exceptions and error codes
- Shared utilities (hashing, token generation, cost calculation)
- Database migration setup (Alembic)

### 2. Auth Component (`src/auth/`)
**Purpose**: Handle all authentication — credential exchange, token management, service account registration.

**Responsibilities**:
- `POST /auth/exchange` — validate AWS creds via STS, resolve tenant, issue token
- Token validation and refresh
- Service account CRUD
- STS client integration
- Token hashing and storage

**Stories**: US-1.4, US-1.5, US-1.6, US-9.1, US-9.2, US-9.5

### 3. Proxy Component (`src/proxy/`)
**Purpose**: Handle all Bedrock proxy requests across 3 API formats.

**Responsibilities**:
- OpenAI chat completions format (`/v1/chat/completions`, `/v1/models`)
- Anthropic Messages format (`/v1/messages`, `/v1/messages/count_tokens`)
- Bedrock InvokeModel pass-through (`/bedrock/invoke`, `/bedrock/invoke-with-response-stream`)
- Request format translation (OpenAI → Bedrock, Anthropic → Bedrock)
- Streaming response handling (SSE)
- Model alias resolution
- Model access control (allowed models per team)

**Stories**: US-4.1, US-4.2, US-4.3, US-9.6

### 4. Budget Component (`src/budget/`)
**Purpose**: Cascading budget management at all hierarchy levels.

**Responsibilities**:
- Budget CRUD at org/department/team/user levels
- Cascading enforcement logic (child cannot exceed parent)
- Cost calculation (tokens × model pricing)
- Budget usage aggregation and tracking
- Soft/hard enforcement modes
- Service account budget separation

**Stories**: US-2.1, US-2.2, US-2.3, US-2.4, US-9.3

### 5. Rate Limiting Component (`src/ratelimit/`)
**Purpose**: Rate limit enforcement with pluggable backends.

**Responsibilities**:
- Rate limit CRUD at all hierarchy levels
- Token bucket algorithm implementation
- In-memory backend (default)
- Redis backend (optional, for multi-instance)
- Backend abstraction (interface-driven)
- Service account rate limit separation

**Stories**: US-3.1, US-3.2, US-3.3

### 6. Bedrock Pool Component (`src/pool/`)
**Purpose**: Manage cross-account Bedrock pool with round-robin routing.

**Responsibilities**:
- Pool configuration loading
- Cross-account IAM role assumption (STS AssumeRole)
- Round-robin request distribution
- Health tracking (mark unhealthy on throttle/error, restore after cooldown)
- Pool status reporting

**Stories**: US-5.1, US-9.4

### 7. Admin API Component (`src/admin/`)
**Purpose**: Admin REST API for platform and org management.

**Responsibilities**:
- Organization CRUD
- Department/team/user management (read from Identity Center, manage policies)
- Budget and rate limit configuration endpoints
- Usage and log query endpoints
- Pool management endpoints
- Role-based access control (platform admin vs org admin vs dept admin)

**Stories**: US-1.2, US-1.3, US-7.2, US-7.3, US-7.4

### 8. Usage Tracking Component (`src/usage/`)
**Purpose**: Request logging, metrics, and usage aggregation.

**Responsibilities**:
- Log every request to PostgreSQL
- Prometheus metrics endpoint (`/metrics`)
- Usage aggregation queries (by org, dept, team, user, time range)
- Optional CloudWatch integration
- Health and readiness endpoints

**Stories**: US-8.1, US-8.2

### 9. Admin UI Component (`frontend/`)
**Purpose**: React + Tailwind admin dashboard.

**Responsibilities**:
- SSO login flow (AWS credential exchange in browser)
- Platform admin dashboard (orgs, pool health, system metrics)
- Org admin dashboard (departments, teams, users, budgets, usage)
- Department admin view (team budgets, usage)
- Log viewer with filters
- Configuration management UI
- Claude Code setup page (helper script download)

**Stories**: US-6.3, US-7.1, US-7.2, US-7.3, US-7.4

### 10. Infrastructure Component (`infra/`)
**Purpose**: Terraform modules for EKS, RDS, ALB, and supporting infrastructure.

**Responsibilities**:
- EKS Auto Mode cluster configuration
- RDS PostgreSQL instance
- ALB with HTTPS
- ElastiCache Redis (optional)
- ECR repository
- IAM roles (gateway service role, cross-account roles)
- Security groups and networking
- GitHub Actions workflow files

**Stories**: US-1.1

### 11. CLI Tools Component (`cli/`)
**Purpose**: Client-side helper scripts for Claude Code integration.

**Responsibilities**:
- `bg-auth.sh` — credential exchange helper for Claude Code's `apiKeyHelper`
- `install.sh` — installs helper to user's machine
- `claude-settings.example.json` — example Claude Code configuration
- Container examples (Dockerfile, K8s manifest for M2M)

**Stories**: US-6.1, US-6.2
