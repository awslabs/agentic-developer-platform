# Bedrock Gateway

A multi-tenant SaaS proxy for Amazon Bedrock with Cognito authentication, cascading budget management, rate limiting, and an admin dashboard.

## What It Does

Bedrock Gateway gives organizations controlled, metered access to Amazon Bedrock models. Developers use Claude Code, Cursor, Continue, or any OpenAI-compatible client — the gateway handles authentication, tenant isolation, budgets, rate limits, and audit logging transparently.

Think of it as a corporate API gateway purpose-built for LLM traffic: one endpoint, many tenants, full cost visibility.

## Architecture

```
                              Internet
                                 │
                    ┌────────────▼────────────┐
                    │       CloudFront        │
                    │   (HTTPS termination,   │
                    │    VPC Origin)          │
                    └────────────┬────────────┘
                                 │
                ┌────────────────┼────────────────┐
                │                                 │
    ┌───────────▼───────────┐      ┌──────────────▼──────────────┐
    │    S3 (Admin UI)      │      │    Internal ALB (EKS)       │
    │   React + Tailwind    │      │  via VPC Origin — HTTP      │
    └───────────────────────┘      └──────────────┬──────────────┘
                                                  │
                                   ┌──────────────▼──────────────┐
                                   │     Bedrock Gateway         │
                                   │     (FastAPI on EKS)        │
                                   └──────────────┬──────────────┘
                                                  │
                    ┌─────────────────────────────┼─────────────────────────────┐
                    │                             │                             │
         ┌──────────▼──────────┐      ┌──────────▼───────────┐      ┌──────────▼──────────┐
         │   RDS PostgreSQL    │      │   ElastiCache Redis   │      │   Amazon Bedrock    │
         │   (Multi-tenant)    │      │   (Rate Limiting)     │      │   (Claude, etc.)    │
         └─────────────────────┘      └──────────────────────┘      └─────────────────────┘
```

### How a Request Flows

1. Client sends a request (OpenAI, Anthropic, or Bedrock format) to CloudFront
2. CloudFront routes `/api/*` to the internal ALB via VPC Origin (never internet-facing)
3. FastAPI validates the JWT (Cognito JWKS, no network call needed)
4. Tenant is resolved from the token claims → org/department/team/user hierarchy
5. Budget check: cascading limits at any hierarchy level (soft warn, hard reject)
6. Rate limit check: RPM, TPM, concurrent requests via Redis token bucket
7. Model resolution: maps requested model to a Bedrock endpoint (supports cross-account pool)
8. Bedrock invocation: streaming or synchronous, with per-segment timing
9. Usage logged to Postgres; chat content optionally logged to S3
10. Response returned with `X-Gateway-Timing` header showing per-segment latency

## Key Components

### Backend (`src/`)

| Package | Responsibility |
|---------|---------------|
| `proxy/` | Multi-format Bedrock proxy — translates OpenAI, Anthropic Messages, and native Bedrock formats. Handles streaming via SSE. |
| `auth/` | Cognito JWT validation, service account management, tenant resolution, magic-link auth, vault credential injection |
| `admin/` | CRUD API for organizations, departments, teams, users, agents. Policy scoping, identity index, metrics. |
| `budget/` | Cascading budget enforcement at org/dept/team/user level. Soft (warn) and hard (reject) thresholds. Pricing engine per model. |
| `ratelimit/` | Token bucket algorithm backed by Redis. Per-entity RPM, TPM, and concurrent request limits. |
| `pool/` | Cross-account Bedrock pool — round-robin calls across multiple AWS accounts for throughput scaling. Health tracking per account. |
| `chat_logging/` | Optional conversation logging to S3. PII scrubbing via Comprehend. Configurable scrub levels (off/basic/standard). |
| `usage/` | Usage analytics — per-user, per-model, per-org token consumption and cost tracking. |
| `shared/` | Database, config, logging, metrics, timing, tracing infrastructure. |

### Frontend (`frontend/`)

React + Tailwind admin dashboard. Provides:
- Organization/team/user management
- Budget configuration and status monitoring
- Rate limit configuration
- Usage analytics and cost dashboards
- Agent onboarding and credential management

### Infrastructure (`infra/`)

Terraform modules (14 total):

| Module | What It Creates |
|--------|----------------|
| `cognito/` | User Pool, Identity Pool, App Clients (PKCE + M2M) |
| `cloudfront/` | CDN distribution with VPC Origin to internal ALB |
| `rds/` | PostgreSQL with IAM auth, automated backups |
| `redis/` | ElastiCache for rate limiting state |
| `s3-frontend/` | Bucket for React SPA |
| `s3-chat-logs/` | Bucket for conversation logs |
| `api-gateway/` | REST API Gateway with 15-min timeout (optional, for long-running agent calls) |
| `lambda-authorizer/` | JWT + IAM auth for API Gateway routes |
| `budget-lambda/` | Usage aggregation Lambdas |
| `cloudwatch-dashboard/` | Latency and error dashboards |
| `alb/` | Internal Application Load Balancer |
| `s3-cloudfront-logs/` | Access log storage |
| `rds-bootstrap/` | K8s Job that grants IAM auth to the Postgres admin role |
| `github-auth-broker/` | Lambda for GitHub OAuth federated sign-in |

### Kubernetes (`k8s/`)

- `deployment.yaml` — FastAPI pods (2 replicas default, HPA-ready)
- `service.yaml` — ClusterIP service
- `ingress.yaml` — ALB Ingress (internal, used by CloudFront VPC Origin)
- `configmap.yaml` — Environment configuration
- `pdb.yaml` — Pod disruption budget
- `namespace.yaml` — `adp-gateway` namespace

## Authentication

Three methods, one token format:

| Method | Use Case | Flow |
|--------|----------|------|
| Email/password | Default for all users | Cognito User Pool sign-in |
| GitHub SSO | Teams using GitHub identity | Cognito federated via GitHub OAuth App |
| Client credentials (M2M) | Automated agents and services | Cognito App Client with `client_credentials` grant |

All methods produce Cognito JWTs. The backend validates them identically regardless of how they were issued.

### Claude Code Integration

```bash
# Install the CLI helper
cp cli/bg-cognito-auth.sh ~/bin/
chmod +x ~/bin/bg-cognito-auth.sh

# One-time login
bg-cognito-auth.sh login --gateway-url https://<cloudfront-domain>/api

# Configure Claude Code (~/.claude/settings.json)
{
  "env": {
    "ANTHROPIC_BEDROCK_BASE_URL": "https://<cloudfront-domain>/api",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1"
  },
  "apiKeyHelper": "~/bin/bg-cognito-auth.sh token",
  "apiKeyHelperTtlMs": 3300000
}
```

## API Surface

### Proxy (Multi-Format)

| Method | Path | Format |
|--------|------|--------|
| POST | `/v1/chat/completions` | OpenAI-compatible |
| POST | `/v1/messages` | Anthropic Messages |
| POST | `/v1/messages/count_tokens` | Anthropic token counting |
| POST | `/bedrock/invoke` | Bedrock native pass-through |
| POST | `/bedrock/invoke-with-response-stream` | Bedrock streaming |
| GET | `/v1/models` | List available models |

### Admin, Budgets, Rate Limits

| Area | Key Endpoints |
|------|---------------|
| Admin | `POST /admin/organizations`, `POST /admin/agents`, `GET /admin/pool/status` |
| Budgets | `POST /budgets`, `GET /budgets/status/{entity_type}/{entity_id}` |
| Rate Limits | `PUT /ratelimits/{entity_type}/{entity_id}`, `GET /ratelimits/{entity_type}/{entity_id}/status` |
| Auth | `GET /auth/me`, `POST /auth/service-accounts` |
| Health | `GET /health` |

Full specification: [docs/openapi.yaml](docs/openapi.yaml)

## Deployment

### Automated (recommended)

The gateway deploys as part of the full platform via `deploy-all.sh`:

```bash
# From repo root — deploys everything including gateway
./platform/scripts/deploy-all.sh

# Gateway-only (skips agent-factory)
./platform/scripts/deploy-all.sh --gateway-only
```

The script handles: Terraform apply (two-pass for ALB wiring) → CodeBuild image build → EKS rollout → frontend build → S3 upload → CloudFront invalidation.

### Manual Step-by-Step

```bash
# 1. Infrastructure
cd modules/gateway/infra
terraform init -backend-config=../../../environments/dev/modules/gateway-backend.tfvars
terraform apply -var-file=../../../environments/dev/modules/gateway.tfvars -auto-approve

# 2. Backend image
docker build -t adp-gateway .
REGISTRY="$(aws sts get-caller-identity --query Account --output text).dkr.ecr.us-east-1.amazonaws.com"
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $REGISTRY
docker tag adp-gateway:latest $REGISTRY/adp-gateway:latest
docker push $REGISTRY/adp-gateway:latest

# 3. Deploy to EKS
kubectl apply -f k8s/ -n adp-gateway
kubectl rollout status deployment/bedrockgateway -n adp-gateway --timeout=300s

# 4. Frontend
cd frontend && npm ci
VITE_API_URL="/api/gateway" npm run build
BUCKET=$(aws ssm get-parameter --name "/adp/dev/gateway/frontend-bucket" --query "Parameter.Value" --output text)
aws s3 sync dist/ "s3://${BUCKET}/" --delete
```

### CI/CD Workflows

| Workflow | Trigger | What It Does |
|----------|---------|--------------|
| `gateway-ci.yml` | PR to `src/`, `tests/`, `pyproject.toml` | Lint → Test → Docker build |
| `gateway-deploy.yml` | Push to main | ECR push → EKS rollout → S3 sync → CF invalidation |
| `gateway-infra-apply.yml` | Push to `infra/**` | Terraform plan (PR) / apply (merge) with two-pass ALB wiring |

## Local Development

```bash
cd modules/gateway

# Full stack (backend + Postgres + Redis)
docker compose up
# Backend at http://localhost:8080, Postgres at :5432, Redis at :6379

# Backend only (requires running Postgres + Redis)
uv sync
uvicorn src.app:create_app --factory --reload --port 8080

# Tests
uv run pytest tests/ -v

# Lint
uv run ruff check src/ tests/
uv run ruff format src/ tests/

# Frontend dev
cd frontend && npm install && npm run dev  # http://localhost:5173
```

## Security Model

| Layer | Mechanism |
|-------|-----------|
| Network | Internal ALB — not internet-facing. CloudFront VPC Origin is the only ingress path. |
| Authentication | Cognito JWT validated via JWKS (cached, no network call per request) |
| AWS Access | IRSA — pods use IAM Roles for Service Accounts, no static credentials |
| Database | RDS IAM Auth — no passwords stored or rotated |
| Tenant Isolation | All queries include `org_id` filter; logical isolation at the application layer |
| Credentials | AWS credentials used once for STS validation, never stored. Secrets in Secrets Manager only. |
| Audit | CloudTrail for infrastructure; application-level usage logging per request |

## Observability

- `X-Gateway-Timing` header on every response: `auth=5ms;budget_check=12ms;bedrock=1847ms;total=1870ms`
- Optional X-Ray distributed tracing via OpenTelemetry (set `BG_OTEL_ENABLED=true`)
- CloudWatch dashboard for P50/P90/P99 latency, error rates, request counts
- Structured JSON logging with correlation IDs

## Further Reading

- [OpenAPI Specification](docs/openapi.yaml)
- [Database Schema](docs/database-schema.md)
- [Sequence Diagrams](docs/sequence-diagrams.md)
- [Security Review](docs/security-review.md)
- [Budget & Rate Limiting Design](docs/budget-ratelimit.md)
- [GitHub Sign-In Admin Guide](../../docs/admin/github-sign-in.md)
