# Units of Work

## Unit 0: Shared Foundation (Built on this machine — NOT an agent unit)

**Scope**: `src/shared/`, `src/app.py`, `alembic/`, `pyproject.toml`, `Dockerfile`, `docker-compose.yml`

**Purpose**: Common code all units depend on. Committed to `main` before agents start.

**Includes**:
- SQLAlchemy ORM models (all 13 tables)
- Pydantic schemas (all request/response models)
- Service interfaces (ABCs for all services)
- `app.py` with middleware chain and auto-discovery router registration
- Configuration (Pydantic BaseSettings)
- Shared utilities (hash_token, generate_token, calculate_cost)
- Custom exceptions with error codes
- Database setup (async engine, session factory)
- Alembic migrations (initial schema)
- `pyproject.toml` with ALL backend dependencies pinned to exact versions
- `frontend/package.json` with ALL frontend dependencies pinned to exact versions
- `infra/versions.tf` with Terraform and provider version constraints
- `Dockerfile` (multi-stage build)
- `docker-compose.yml` (PostgreSQL + Redis + app for local dev)
- `aidlc-docs/inception/application-design/technology-stack.md` — version reference for agents

**Agent Dependency Rule**: Agents MUST NOT add new dependencies. If a library is needed, raise a ❓ Clarification Request.

**Auto-Discovery Pattern**:
```python
# src/app.py — agents never modify this file
UNIT_MODULES = [
    "src.auth.routes",
    "src.proxy.routes",
    "src.admin.routes",
    "src.pool.routes",
    "src.budget.routes",
    "src.ratelimit.routes",
    "src.usage.routes",
]
# Auto-imports router from each module, skips if not yet implemented
```

---

## Unit 1: Auth

**Scope**: `src/auth/`, `tests/auth/`
**Agent**: Yes
**Stories**: US-1.4, US-1.5, US-1.6, US-9.1, US-9.2, US-9.5

**Delivers**:
- `AuthService` implementing `IAuthService`
- `ServiceAccountService` for CRUD
- `TenantResolver` — STS identity → org/dept/team mapping
- `src/auth/routes.py` with `router` (POST /auth/exchange)
- STS client integration (GetCallerIdentity)
- Token generation, hashing, storage, validation
- Unit tests with mocked STS

**Interfaces Implemented**: `IAuthService`
**Depends On**: Shared foundation only
**Independently Testable**: Yes — mock STS responses

---

## Unit 2: Bedrock Pool

**Scope**: `src/pool/`, `tests/pool/`
**Agent**: Yes
**Stories**: US-5.1, US-9.4

**Delivers**:
- `PoolService` implementing `IPoolService`
- Cross-account IAM role assumption (STS AssumeRole)
- Round-robin distribution logic
- Health tracking with cooldown
- `src/pool/routes.py` with `router` (GET /admin/pool/status — optional, can also be in admin)
- Unit tests with mocked STS and Bedrock clients

**Interfaces Implemented**: `IPoolService`
**Depends On**: Shared foundation only
**Independently Testable**: Yes — mock AWS clients

---

## Unit 3: Proxy

**Scope**: `src/proxy/`, `tests/proxy/`
**Agent**: Yes
**Stories**: US-4.1, US-4.2, US-4.3, US-9.6

**Delivers**:
- `ProxyService` implementing `IProxyService`
- `FormatTranslator` — OpenAI↔Bedrock, Anthropic↔Bedrock
- `ModelResolver` — alias resolution, access control
- `src/proxy/routes.py` with `router` (/v1/chat/completions, /v1/messages, /v1/models, /bedrock/invoke, /bedrock/invoke-with-response-stream)
- SSE streaming response handling
- Header/body field preservation (anthropic_beta, anthropic_version)
- Unit tests with mocked IPoolService

**Interfaces Implemented**: `IProxyService`
**Depends On**: Shared foundation, `IPoolService` (via ABC — mock in tests)
**Independently Testable**: Yes — mock pool service

---

## Unit 4: Budget

**Scope**: `src/budget/`, `tests/budget/`
**Agent**: Yes
**Stories**: US-2.1, US-2.2, US-2.3, US-2.4, US-9.3

**Delivers**:
- `BudgetService` implementing `IBudgetService`
- Cascading enforcement logic (user → team → dept → org)
- Cascade validation (child ≤ parent)
- Cost calculation using model pricing
- Soft/hard enforcement modes
- Service account budget separation
- `src/budget/routes.py` with `router` (budget CRUD endpoints)
- Unit tests with in-memory database

**Interfaces Implemented**: `IBudgetService`
**Depends On**: Shared foundation only
**Independently Testable**: Yes — pure business logic with test DB

---

## Unit 5: Rate Limiting

**Scope**: `src/ratelimit/`, `tests/ratelimit/`
**Agent**: Yes
**Stories**: US-3.1, US-3.2, US-3.3

**Delivers**:
- `RateLimitService` implementing `IRateLimitService`
- `RateLimitBackend` interface
- `InMemoryBackend` implementation
- `RedisBackend` implementation
- Token bucket algorithm
- Hierarchy-level enforcement (most restrictive wins)
- Service account rate limit separation
- `src/ratelimit/routes.py` with `router` (rate limit config endpoints)
- Unit tests for both backends (Redis tests use fakeredis)

**Interfaces Implemented**: `IRateLimitService`, `RateLimitBackend`
**Depends On**: Shared foundation only
**Independently Testable**: Yes — in-memory backend for tests, fakeredis for Redis tests

---

## Unit 6: Admin API + Usage

**Scope**: `src/admin/`, `src/usage/`, `tests/admin/`, `tests/usage/`
**Agent**: Yes
**Stories**: US-1.2, US-1.3, US-7.2, US-7.3, US-7.4, US-8.1, US-8.2

**Delivers**:
- Admin REST API routes (org CRUD, budget/ratelimit config, log queries, pool status)
- `AccessControl` — role-based access (platform admin, org admin, dept admin)
- `UsageService` implementing `IUsageService`
- `MetricsService` — Prometheus metrics endpoint
- Request logging to PostgreSQL
- Usage aggregation queries
- Health/readiness endpoints
- `src/admin/routes.py` and `src/usage/routes.py` with routers
- Unit tests with mocked IBudgetService, IRateLimitService

**Interfaces Implemented**: `IUsageService`
**Depends On**: Shared foundation, `IBudgetService`, `IRateLimitService` (via ABCs — mock in tests)
**Independently Testable**: Yes — mock service dependencies

---

## Unit 7: Admin UI

**Scope**: `frontend/`, `tests/frontend/`
**Agent**: Yes
**Stories**: US-6.3, US-7.1, US-7.2, US-7.3, US-7.4

**Delivers**:
- React + Tailwind SPA
- SSO login flow (AWS credential exchange in browser)
- Platform admin dashboard (orgs, pool health, system metrics)
- Org admin dashboard (departments, teams, users, budgets, usage)
- Department admin view
- Log viewer with filters and pagination
- Configuration management pages
- Claude Code setup page (helper script download)
- Unit tests (React Testing Library / Vitest)

**Interfaces Implemented**: None (consumes Admin API via HTTP)
**Depends On**: Admin API contract (OpenAPI spec generated from shared foundation schemas)
**Independently Testable**: Yes — mock API responses with MSW (Mock Service Worker)

---

## Unit 8: Infrastructure

**Scope**: `infra/`
**Agent**: Yes
**Stories**: US-1.1

**Delivers**:
- Terraform modules:
  - `infra/modules/eks/` — EKS Auto Mode cluster
  - `infra/modules/rds/` — PostgreSQL instance
  - `infra/modules/alb/` — ALB with HTTPS, ACM cert
  - `infra/modules/redis/` — ElastiCache Redis (optional)
  - `infra/modules/ecr/` — ECR repository
  - `infra/modules/iam/` — Gateway service role, cross-account assume role policies
  - `infra/modules/networking/` — VPC, subnets, security groups
- `infra/environments/dev/` — dev environment tfvars
- `infra/environments/prod/` — prod environment tfvars
- `infra/main.tf` — root module composing all modules

**Interfaces Implemented**: None
**Depends On**: Nothing (standalone Terraform)
**Independently Testable**: Yes — `terraform validate`, `terraform plan`

---

## Unit 9: DevOps Pipelines

**Scope**: `.github/workflows/`
**Agent**: Yes
**Stories**: (cross-cutting — supports all deployment)

**Delivers**:
- `.github/workflows/infra-plan.yml` — Terraform plan on PR to `infra/`
- `.github/workflows/infra-apply.yml` — Terraform apply on merge to main (infra changes)
- `.github/workflows/backend-ci.yml` — Python lint (ruff), test (pytest), build Docker image
- `.github/workflows/backend-deploy.yml` — Push to ECR, deploy to EKS (rolling update)
- `.github/workflows/frontend-ci.yml` — Node lint, test, build
- `.github/workflows/frontend-deploy.yml` — Build and deploy frontend
- All workflows use EKS self-hosted runners (already available)

**Interfaces Implemented**: None
**Depends On**: Knowledge of project structure (from shared foundation), ECR/EKS names (from infra outputs)
**Independently Testable**: Yes — workflow syntax validation, act (local runner) for testing

---

## Unit 10: CLI Tools

**Scope**: `cli/`
**Agent**: Yes
**Stories**: US-6.1, US-6.2

**Delivers**:
- `cli/bg-auth.sh` — credential exchange helper for Claude Code apiKeyHelper
- `cli/install.sh` — installs bg-auth.sh to ~/bin/
- `cli/claude-settings.example.json` — example Claude Code configuration
- `cli/examples/Dockerfile.agent` — example Dockerfile for M2M containers
- `cli/examples/k8s-agent.yaml` — example Kubernetes manifest for M2M
- `cli/README.md` — setup instructions

**Interfaces Implemented**: None (consumes Auth API via HTTP)
**Depends On**: Auth API contract (POST /auth/exchange request/response format)
**Independently Testable**: Yes — shellcheck for scripts, mock curl responses for integration tests


---

## Unit 11: Integration & E2E Test Generation

**Scope**: `tests/integration/`, `tests/e2e/`, `tests/fixtures/`
**Agent**: Yes (parallel with Units 1-10)
**Stories**: All stories (tests derived from acceptance criteria)

**Delivers**:
- Integration tests verifying cross-unit interactions:
  - Auth → token validation → proxy request flow
  - Budget check → enforcement → usage recording
  - Rate limit check → enforcement → header responses
  - Pool round-robin → failover on throttle
- End-to-end test scenarios mapped from user stories:
  - Full auth flow (US-1.4, US-1.6)
  - Budget cascading (US-2.1, US-2.3)
  - Proxy through all 3 formats (US-4.1, US-4.2, US-4.3)
  - Admin CRUD operations (US-1.3, US-7.2, US-7.3)
  - Error scenarios (US-9.1 through US-9.6)
- Shared test fixtures and factories:
  - `tests/fixtures/factories.py` — create_org, create_user, create_token, create_budget, etc.
  - `tests/fixtures/mock_aws.py` — mocked STS, Bedrock responses
  - `tests/fixtures/seed_data.py` — test data seeding for environments
- `docker-compose.test.yml` — local test environment (PostgreSQL + Redis + app)
- `tests/conftest.py` — shared pytest configuration and fixtures
- `tests/README.md` — how to run tests locally and in CI

**Interfaces Implemented**: None
**Depends On**: Shared foundation (models, schemas, interfaces), user stories (acceptance criteria)
**Independently Testable**: Yes — tests are syntactically valid Python, can verify with `python -m py_compile`

**Note**: This unit generates test CODE only. Tests are not executed until Unit 13 (Test Runner).

---

## Unit 12: Environment Provisioning

**Scope**: Environment creation using Terraform modules from Unit 8
**Agent**: Yes (sequential — after Unit 8 is merged)
**Stories**: (cross-cutting — supports deployment)

**Delivers**:
- `infra/environments/dev/terraform.tfvars` — Dev environment configuration
- `infra/environments/dev/backend.tf` — Dev state backend (S3)
- `infra/environments/test/terraform.tfvars` — Test environment configuration
- `infra/environments/test/backend.tf` — Test state backend (S3)
- `infra/environments/prod/terraform.tfvars` — Prod environment configuration
- `infra/environments/prod/backend.tf` — Prod state backend (S3)
- Executes `terraform apply` for Dev and Test environments
- Documents environment endpoints and access details
- Validates environments are healthy (health check endpoints respond)

**Interfaces Implemented**: None
**Depends On**: Unit 8 (Infrastructure) must be merged — uses its Terraform modules
**Independently Testable**: Yes — `terraform plan` validates before apply

**Note**: Prod environment is provisioned but NOT deployed to until full test suite passes.

---

## Unit 13: Test Runner

**Scope**: Execute all test suites against deployed environments
**Agent**: Yes (sequential — after all code merged + environments provisioned)
**Stories**: (validates all stories)

**Delivers**:
- Executes unit tests for all units: `pytest tests/ -v`
- Executes integration tests against Dev environment: `pytest tests/integration/ -v`
- Executes e2e tests against Dev environment: `pytest tests/e2e/ -v`
- Generates test report (pass/fail, coverage)
- Posts results as GitHub issue comment
- If all tests pass: flags as ready for Prod deployment
- If tests fail: creates GitHub issues for failures with details

**Interfaces Implemented**: None
**Depends On**: All units merged, Unit 11 (test code), Unit 12 (environments provisioned)
**Independently Testable**: N/A — this unit IS the test execution

**Note**: This unit executes tests, it does not write them. Test code comes from Unit 11 + per-unit tests from Units 1-10.
