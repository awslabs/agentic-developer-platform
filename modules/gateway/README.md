# Bedrock Gateway

A multi-tenant SaaS proxy for Amazon Bedrock with Cognito authentication, cascading budget management, rate limiting, and an admin dashboard.

## Overview

Bedrock Gateway enables organizations to provide secure, controlled access to Amazon Bedrock models for their developers and automated agents. It supports Claude Code, OpenAI-compatible tools (Cursor, Continue), and custom applications.

```
                             Internet
                                |
                   +------------v-----------+
                   |       CloudFront       |
                   | (HTTPS, VPC Origin)    |
                   +------------+-----------+
                                |
               +----------------+----------------+
               |                                 |
   +-----------v-----------+      +--------------v--------------+
   |    S3 (Admin UI)      |      |    Internal ALB (EKS)       |
   |   React + Tailwind    |      |  via VPC Origin - HTTP      |
   +------------------------+      +--------------+--------------+
                                                  |
                                   +--------------v--------------+
                                   |     Bedrock Gateway         |
                                   |     (FastAPI on EKS)        |
                                   +--------------+--------------+
                                                  |
                    +-----------------------------+-----------------------------+
                    |                             |                             |
         +----------v----------+      +-----------v-----------+      +----------v----------+
         |   RDS PostgreSQL    |      |   ElastiCache Redis   |      |   Amazon Bedrock    |
         |   (Multi-tenant)    |      |   (Rate Limiting)     |      |   (Claude, etc.)    |
         +---------------------+      +-----------------------+      +---------------------+
```

## Key Features

- **Multi-Tenant Architecture**: Org -> Department -> Team -> User hierarchy with data isolation
- **Unified Authentication**: Cognito PKCE for humans, client_credentials for agents
- **Cascading Budgets**: Set spending limits at any hierarchy level with soft/hard enforcement
- **Rate Limiting**: RPM, TPM, and concurrent request limits with token bucket algorithm
- **Multi-Format API**: OpenAI, Anthropic Messages, and Bedrock pass-through formats
- **Claude Code Integration**: First-class support with CLI authentication helper
- **Admin Dashboard**: React UI for onboarding, configuration, and usage monitoring
- **Cross-Account Pool**: Round-robin Bedrock calls across multiple AWS accounts

## Quick Start

### Prerequisites

- AWS CLI configured with admin access
- Terraform >= 1.5
- kubectl configured for your cluster
- Node.js >= 18 (for frontend development)
- Python >= 3.11 (for backend development)

### 1. Deploy Infrastructure

```bash
cd infra

# Initialize and apply Terraform
terraform init
terraform apply -var="environment=dev"

# Note the outputs:
# - cloudfront_domain_name
# - cognito_user_pool_id
# - cognito_client_id
```

### 2. Deploy Backend

```bash
# Build and push container
docker build -t bedrockgw-backend .
aws ecr get-login-password | docker login --username AWS --password-stdin <account>.dkr.ecr.us-east-1.amazonaws.com
docker tag bedrockgw-backend:latest <account>.dkr.ecr.us-east-1.amazonaws.com/bedrockgw-dev-backend:latest
docker push <account>.dkr.ecr.us-east-1.amazonaws.com/bedrockgw-dev-backend:latest

# Deploy to EKS
kubectl apply -f k8s/
```

### 3. Deploy Frontend

```bash
cd frontend
npm install
npm run build

# Upload to S3
aws s3 sync dist/ s3://bedrockgw-dev-frontend/ --delete

# Invalidate CloudFront cache
aws cloudfront create-invalidation --distribution-id <dist-id> --paths "/*"
```

### 4. Configure Claude Code

```bash
# Install CLI helper
cp cli/bg-cognito-auth.sh ~/bin/
chmod +x ~/bin/bg-cognito-auth.sh

# Login (one-time)
bg-cognito-auth.sh login --gateway-url https://<cloudfront-domain>/api

# Use Claude Code
CLAUDE_CODE_USE_BEDROCK=1 claude
```

## Authentication

### Human Users (Claude Code, Admin UI)

Human users authenticate via Cognito PKCE flow:

1. Run `bg-cognito-auth.sh login` to authenticate with your corporate IdP
2. CLI obtains Cognito tokens and exchanges for AWS credentials via Identity Pool
3. Credentials stored in `~/.aws/credentials` under `[bedrock-gateway]` profile
4. Claude Code uses SigV4 authentication with these credentials

```bash
# One-time login
./cli/bg-cognito-auth.sh login --gateway-url https://gateway.company.com/api

# Check status
./cli/bg-cognito-auth.sh status

# Refresh credentials
./cli/bg-cognito-auth.sh refresh
```

### Automated Agents (M2M)

Agents authenticate using Cognito's `client_credentials` flow:

1. Admin creates an agent via `/admin/agents` API
2. Agent receives `client_id` and `client_secret`
3. Agent calls Cognito token endpoint to get JWT access token
4. JWT used as Bearer token for gateway requests

```bash
# Get access token
TOKEN=$(curl -X POST "https://cognito-idp.us-east-1.amazonaws.com/<pool-id>/oauth2/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials&client_id=<id>&client_secret=<secret>&scope=bedrockgw/invoke")

# Use token
curl -H "Authorization: Bearer $TOKEN" \
  https://gateway.company.com/api/v1/chat/completions
```

## CLI Usage

The `bg-cognito-auth.sh` CLI handles Cognito authentication:

```bash
# Commands
bg-cognito-auth.sh login    # Authenticate and get AWS credentials
bg-cognito-auth.sh refresh  # Refresh tokens and credentials
bg-cognito-auth.sh status   # Show authentication status
bg-cognito-auth.sh logout   # Clear stored credentials
bg-cognito-auth.sh token    # Output access token (for apiKeyHelper)

# Options
--gateway-url <url>         # Gateway URL (required for login)
--user-pool-id <id>         # Cognito User Pool ID (auto-discovered)
--client-id <id>            # Cognito App Client ID (auto-discovered)
--region <region>           # AWS region (default: us-east-1)
```

### Claude Code Configuration

Add to `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BEDROCK_BASE_URL": "https://your-gateway.cloudfront.net/api",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1"
  },
  "apiKeyHelper": "~/bin/bg-cognito-auth.sh token",
  "apiKeyHelperTtlMs": 3300000
}
```

## Project Structure

```
bedrock-gateway/
├── src/                    # Backend Python code
│   ├── auth/               # Authentication (Cognito JWT, service accounts)
│   ├── proxy/              # Bedrock proxy (OpenAI, Anthropic, Bedrock formats)
│   ├── admin/              # Admin CRUD API
│   ├── budget/             # Budget tracking and enforcement
│   ├── ratelimit/          # Rate limiting (token bucket, Redis)
│   ├── usage/              # Usage logging and analytics
│   ├── pool/               # Cross-account Bedrock pool
│   └── shared/             # Shared models, schemas, utilities
├── tests/                  # Backend tests (pytest)
├── frontend/               # Admin UI (React + Tailwind)
├── infra/                  # Terraform infrastructure
│   ├── main.tf             # Root module
│   └── modules/            # Terraform modules
│       ├── cognito/        # User Pool, Identity Pool, App Clients
│       ├── cloudfront/     # Distribution with VPC Origin
│       ├── eks/            # EKS Auto Mode cluster
│       ├── rds/            # PostgreSQL database
│       ├── redis/          # ElastiCache Redis
│       └── ...
├── k8s/                    # Kubernetes manifests
│   ├── deployment.yaml     # Gateway deployment
│   ├── service.yaml        # ClusterIP service
│   ├── ingress.yaml        # ALB Ingress (internal)
│   └── configmap.yaml      # Environment configuration
├── cli/                    # CLI tools
│   └── bg-cognito-auth.sh  # Cognito authentication CLI
├── docs/                   # Documentation
│   ├── openapi.yaml        # OpenAPI 3.0 specification
│   ├── database-schema.md  # Database table documentation
│   ├── sequence-diagrams.md # Mermaid sequence diagrams
│   └── traceability-matrix.md # Requirements to code mapping
├── aidlc-docs/             # AI-DLC process documents
│   └── inception/          # Requirements, user stories, architecture
├── agent_learning/         # Operational learnings for AI agents
└── .github/workflows/      # CI/CD pipelines
```

## Development

### Backend

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e ".[dev]"

# Run locally
uvicorn src.main:app --reload --port 8080

# Run tests
pytest tests/ -v

# Lint
ruff check src/ tests/
ruff format src/ tests/
```

### Frontend

```bash
cd frontend

# Install dependencies
npm install

# Development server
npm run dev

# Build for production
npm run build

# Run tests
npm test

# Lint
npm run lint
```

## API Endpoints

### Proxy (Multi-Format)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/chat/completions` | OpenAI-compatible chat completions |
| GET | `/v1/models` | List available models |
| POST | `/v1/messages` | Anthropic Messages format |
| POST | `/v1/messages/count_tokens` | Count tokens (Anthropic) |
| POST | `/bedrock/invoke` | Bedrock InvokeModel pass-through |
| POST | `/bedrock/invoke-with-response-stream` | Bedrock streaming |

### Authentication

| Method | Path | Description |
|--------|------|-------------|
| POST | `/auth/exchange` | Exchange AWS creds for token (deprecated) |
| GET | `/auth/me` | Get current user info |
| POST | `/auth/logout` | Logout current user |
| POST | `/auth/service-accounts` | Create service account |

### Admin

| Method | Path | Description |
|--------|------|-------------|
| POST | `/admin/organizations` | Create organization |
| GET | `/admin/organizations/{org_id}` | Get organization |
| POST | `/admin/agents` | Create M2M agent |
| GET | `/admin/pool/status` | Get Bedrock pool status |

### Budgets

| Method | Path | Description |
|--------|------|-------------|
| POST | `/budgets` | Create budget config |
| GET | `/budgets/status/{entity_type}/{entity_id}` | Get budget status |

### Rate Limits

| Method | Path | Description |
|--------|------|-------------|
| PUT | `/ratelimits/{entity_type}/{entity_id}` | Configure rate limits |
| GET | `/ratelimits/{entity_type}/{entity_id}/status` | Get rate limit status |

See [docs/openapi.yaml](docs/openapi.yaml) for the complete API specification.

## Deployment

### Infrastructure (Terraform)

```bash
cd infra

# Plan changes
terraform plan -var="environment=dev"

# Apply changes
terraform apply -var="environment=dev"

# Destroy (caution!)
terraform destroy -var="environment=dev"
```

### Backend (GitHub Actions)

The `backend-deploy.yml` workflow:
1. Runs on push to `main` when `src/` changes
2. Builds Docker image
3. Pushes to ECR
4. Updates EKS deployment
5. Creates/updates CloudFront VPC Origin

### Frontend (GitHub Actions)

The `frontend-deploy.yml` workflow:
1. Runs on push to `main` when `frontend/` changes
2. Builds React app
3. Uploads to S3
4. Invalidates CloudFront cache

## Detailed Documentation

- [AWS Architecture](aidlc-docs/inception/application-design/aws-architecture.md) - Current cloud architecture
- [OpenAPI Specification](docs/openapi.yaml) - Complete API reference
- [Database Schema](docs/database-schema.md) - All tables and relationships
- [Sequence Diagrams](docs/sequence-diagrams.md) - Key flow visualizations
- [Requirements Traceability](docs/traceability-matrix.md) - Requirements to code mapping
- [Requirements Document](aidlc-docs/inception/requirements/requirements.md) - Functional requirements
- [User Stories](aidlc-docs/inception/user-stories/stories.md) - User story definitions

## Distributed Request Tracing (Issue #144)

### Phase 1: X-Gateway-Timing Header

Every proxy response includes an `X-Gateway-Timing` header with per-segment latency breakdown:

```
X-Gateway-Timing: auth=5ms;model_resolve=1ms;budget_check=12ms;ratelimit_check=3ms;bedrock=1847ms;serialize=2ms;total=1870ms
```

Segments tracked:
- `auth` — JWT validation (Cognito JWKS lookup + token verification)
- `model_resolve` — Model alias resolution + access check
- `budget_check` — Budget enforcement middleware (DB query)
- `ratelimit_check` — Rate limit enforcement middleware
- `bedrock` — Actual Bedrock InvokeModel API call
- `serialize` — Response parsing and serialization
- `total` — End-to-end gateway time

The timing breakdown is also included in structured JSON logs for each request.

### Phase 2: AWS X-Ray Distributed Tracing (Opt-in)

Full distributed tracing with X-Ray via OpenTelemetry. Enable by setting:

```bash
# Environment variables
BG_OTEL_ENABLED=true
BG_OTEL_SERVICE_NAME=bedrock-gateway
BG_OTEL_EXPORTER_ENDPOINT=http://localhost:4317
```

Requires:
- OpenTelemetry Collector sidecar (see `k8s/otel-collector-config.yaml`)
- X-Ray IAM permissions (set `enable_xray_tracing = true` in Terraform)
- Install tracing dependencies: `pip install ".[tracing]"`

## Security

- **Internal ALB**: Load balancer is not internet-facing; CloudFront VPC Origin is the only ingress
- **Cognito Authentication**: JWT validation via JWKS (no network call required)
- **IRSA**: EKS pods use IAM Roles for Service Accounts for AWS access
- **RDS IAM Auth**: Database authentication via IAM (no passwords)
- **Tenant Isolation**: All queries include `org_id` filter; data is logically isolated
- **Credential Handling**: AWS credentials used once for STS validation, never stored

## License

Private - Internal use only.
