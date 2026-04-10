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

## Prerequisites

- AWS CLI v2 configured with admin access
- Terraform >= 1.14
- Docker
- kubectl
- Node.js >= 22 (frontend)
- Python >= 3.12 (backend)
- GitHub CLI (`gh`) — for CI/CD workflow management

## Setup Guide — From Scratch to Production

This guide walks through the complete setup: bootstrapping the Terraform state backend, provisioning infrastructure, deploying the application, and configuring CI/CD.

### Step 0: Clone the Repository

```bash
git clone https://github.com/aws-e/adp.git
cd adp/modules/gateway
```

### Step 1: Bootstrap Terraform State Backend

Before any Terraform commands can run, you need an S3 bucket for state storage and a DynamoDB table for state locking. The repo includes a bootstrap script that creates these.

```bash
# From the repo root
cd platform/scripts

# Set your target region and environment
export AWS_REGION=us-east-1
export ENVIRONMENT=dev

# Run the bootstrap script (requires AWS CLI with admin access)
./bootstrap.sh
```

This script will:
1. Detect your AWS account ID via `aws sts get-caller-identity`
2. Create S3 bucket `adp-terraform-state-<ACCOUNT_ID>` with versioning, encryption, and public access block
3. Create DynamoDB table `adp-terraform-locks` (PAY_PER_REQUEST)
4. Replace `ACCOUNT_ID` placeholders in all `environments/**/*.tfvars` files

After bootstrap, verify:
```bash
aws s3 ls | grep adp-terraform-state
aws dynamodb describe-table --table-name adp-terraform-locks --query 'Table.TableStatus'
```

### Step 2: Deploy Platform Infrastructure (Optional)

If you don't already have a shared VPC and EKS cluster, deploy the platform-level infrastructure first:

```bash
cd platform/infra

terraform init -backend-config=../../environments/dev/backend.tfvars
terraform plan -var-file=../../environments/dev/platform.tfvars
terraform apply -var-file=../../environments/dev/platform.tfvars
```

This creates the base VPC, EKS cluster, ECR repositories, and IAM roles. Review `environments/dev/platform.tfvars` to customize:

```hcl
environment = "dev"
aws_region  = "us-east-1"
vpc_cidr    = "10.0.0.0/16"
eks_node_instance_types = ["m5.large", "m5.xlarge"]
eks_node_desired_size   = 2
```

### Step 3: Deploy Gateway Infrastructure

The gateway module provisions its own resources (Cognito, RDS, Redis, CloudFront, S3, etc.) on top of the platform.

```bash
cd modules/gateway/infra

terraform init -backend-config=../../../environments/dev/modules/gateway-backend.tfvars
terraform plan -var-file=../../../environments/dev/modules/gateway.tfvars
terraform apply -var-file=../../../environments/dev/modules/gateway.tfvars
```

Review `environments/dev/modules/gateway.tfvars` to customize:

```hcl
environment           = "dev"
aws_region            = "us-east-1"
rds_instance_class    = "db.t3.medium"
rds_allocated_storage = 20
redis_node_type       = "cache.t3.micro"
cognito_domain_prefix = "adp-gateway-dev"
```

Note the Terraform outputs — you'll need these for the next steps:
- `cloudfront_domain_name`
- `cognito_user_pool_id`
- `cognito_client_id`
- `ecr_repository_url`

**Infrastructure modules provisioned:**

| Module | Resources |
|--------|-----------|
| `networking` | VPC, subnets, security groups |
| `eks` | EKS Auto Mode cluster, node groups, IRSA |
| `rds` | PostgreSQL with IAM auth |
| `redis` | ElastiCache Redis (optional) |
| `cognito` | User Pool, Identity Pool, App Clients |
| `cloudfront` | CDN with VPC Origin to internal ALB |
| `s3-frontend` | S3 bucket for React SPA |
| `ecr` | Container registry |
| `cloudtrail` | Audit logging |
| `cloudwatch-dashboard` | Latency dashboard |
| `s3-chat-logs` | Chat log storage (optional) |
| `budget-lambda` | Usage tracking Lambdas (optional) |
| `api-gateway` | REST API with 15min timeout (optional) |
| `lambda-authorizer` | JWT + IAM auth for API Gateway (optional) |

### Step 4: Deploy the Backend

```bash
cd modules/gateway

# Build the container
docker build -t adp-gateway .

# Push to ECR
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com"

aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $REGISTRY
docker tag adp-gateway:latest $REGISTRY/adp-gateway:latest
docker push $REGISTRY/adp-gateway:latest

# Configure kubectl
aws eks update-kubeconfig --name adp-dev-eks --region us-east-1

# Create the secrets (token secret from Secrets Manager)
kubectl create secret generic bedrockgateway-secrets \
  --from-literal=token-secret-key=$(aws secretsmanager get-secret-value \
    --secret-id /adp/dev/gateway/token-secret --query SecretString --output text) \
  -n adp-gateway

# Deploy k8s manifests
kubectl apply -f k8s/ -n adp-gateway

# Verify rollout
kubectl rollout status deployment/bedrockgateway -n adp-gateway --timeout=300s
```

### Step 5: Deploy the Frontend

```bash
cd modules/gateway/frontend

npm ci
VITE_API_URL="/api/gateway" npm run build

# Upload to S3 (get bucket name from Terraform output or SSM)
BUCKET=$(aws ssm get-parameter --name "/adp/dev/gateway/frontend-bucket" --query "Parameter.Value" --output text)
aws s3 sync dist/ "s3://${BUCKET}/" --delete

# Invalidate CloudFront cache
DIST_ID=$(aws ssm get-parameter --name "/adp/dev/gateway/cloudfront-id" --query "Parameter.Value" --output text)
aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*"
```

### Step 6: Configure GitHub Actions CI/CD

The repo includes three workflows for automated builds and deployments. To enable them:

1. **Create an IAM OIDC identity provider** for GitHub Actions in your AWS account:
   ```bash
   aws iam create-open-id-connect-provider \
     --url https://token.actions.githubusercontent.com \
     --client-id-list sts.amazonaws.com \
     --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
   ```

2. **Create an IAM role** for GitHub Actions with trust policy for your repo:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Principal": {
         "Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"
       },
       "Action": "sts:AssumeRoleWithWebIdentity",
       "Condition": {
         "StringEquals": {
           "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
         },
         "StringLike": {
           "token.actions.githubusercontent.com:sub": "repo:aws-e/adp:*"
         }
       }
     }]
   }
   ```
   Attach policies for ECR, EKS, S3, CloudFront, SSM, and Terraform state access.

3. **Set the GitHub repository secret**:
   ```bash
   gh secret set AWS_ROLE_ARN --body "arn:aws:iam::<ACCOUNT_ID>:role/<role-name>" --repo aws-e/adp
   ```

**Workflow summary:**

| Workflow | File | Trigger | What it does |
|----------|------|---------|-------------|
| Gateway CI | `gateway-ci.yml` | PR/push to `src/`, `tests/`, `pyproject.toml` | Lint → Test → Docker build |
| Gateway Deploy | `gateway-deploy.yml` | Push to main (`src/`, `Dockerfile`, `k8s/`) | ECR push → EKS deploy → S3 sync → CF invalidation |
| Gateway Infra | `gateway-infra.yml` | Push/PR to `infra/**` | Terraform plan (PR) / apply (merge) |
| Platform Infra Plan | `platform-infra-plan.yml` | Platform infra changes | Terraform plan |
| Platform Infra Apply | `platform-infra-apply.yml` | Platform infra changes | Terraform apply |

### Step 7: Configure Claude Code

```bash
# Install CLI helper
cp cli/bg-cognito-auth.sh ~/bin/
chmod +x ~/bin/bg-cognito-auth.sh

# Login (one-time)
bg-cognito-auth.sh login --gateway-url https://<cloudfront-domain>/api

# Use Claude Code
CLAUDE_CODE_USE_BEDROCK=1 claude
```

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

## Local Development (Quick Path)

For local development without deploying infrastructure:

```bash
cd modules/gateway

# Start backend + Postgres + Redis
docker compose up

# Backend is available at http://localhost:8080
# Postgres at localhost:5432 (postgres/postgres)
# Redis at localhost:6379
```

For backend development without Docker:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run locally (requires Postgres and Redis running)
uvicorn src.app:create_app --factory --reload --port 8080

# Run tests
pytest tests/ -v

# Lint
ruff check src/ tests/
ruff format src/ tests/
```

For frontend development:

```bash
cd frontend
npm install
npm run dev       # Dev server at http://localhost:5173
npm test          # Run tests
npm run lint      # Lint
npm run build     # Production build
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
│   ├── configmap.yaml      # Environment configuration
│   ├── pdb.yaml            # Pod disruption budget
│   └── namespace.yaml      # Namespace definition
├── cli/                    # CLI tools
│   └── bg-cognito-auth.sh  # Cognito authentication CLI
├── docs/                   # Documentation
│   ├── openapi.yaml        # OpenAPI 3.0 specification
│   ├── database-schema.md  # Database table documentation
│   ├── sequence-diagrams.md # Mermaid sequence diagrams
│   └── traceability-matrix.md # Requirements to code mapping
├── alembic/                # Database migrations
│   └── versions/           # Migration scripts
├── docker-compose.yml      # Local development stack
├── Dockerfile              # Production container build
└── .github/workflows/      # CI/CD pipelines
    ├── gateway-ci.yml      # Lint, test, build on PR
    ├── gateway-deploy.yml  # Deploy backend + frontend on merge
    └── gateway-infra.yml   # Terraform plan/apply
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

## Distributed Request Tracing

### X-Gateway-Timing Header

Every proxy response includes an `X-Gateway-Timing` header with per-segment latency breakdown:

```
X-Gateway-Timing: auth=5ms;model_resolve=1ms;budget_check=12ms;ratelimit_check=3ms;bedrock=1847ms;serialize=2ms;total=1870ms
```

### AWS X-Ray Distributed Tracing (Opt-in)

Full distributed tracing with X-Ray via OpenTelemetry. Enable by setting:

```bash
BG_OTEL_ENABLED=true
BG_OTEL_SERVICE_NAME=bedrock-gateway
BG_OTEL_EXPORTER_ENDPOINT=http://localhost:4317
```

Requires:
- OpenTelemetry Collector sidecar (see `k8s/otel-collector-config.yaml`)
- X-Ray IAM permissions (set `enable_xray_tracing = true` in Terraform)
- Tracing dependencies: `pip install ".[tracing]"`

## Security

- **Internal ALB**: Load balancer is not internet-facing; CloudFront VPC Origin is the only ingress
- **Cognito Authentication**: JWT validation via JWKS (no network call required)
- **IRSA**: EKS pods use IAM Roles for Service Accounts for AWS access
- **RDS IAM Auth**: Database authentication via IAM (no passwords)
- **Tenant Isolation**: All queries include `org_id` filter; data is logically isolated
- **Credential Handling**: AWS credentials used once for STS validation, never stored

## Detailed Documentation

- [OpenAPI Specification](docs/openapi.yaml) - Complete API reference
- [Database Schema](docs/database-schema.md) - All tables and relationships
- [Sequence Diagrams](docs/sequence-diagrams.md) - Key flow visualizations
- [Requirements Traceability](docs/traceability-matrix.md) - Requirements to code mapping
- [Security Review](docs/security-review.md) - Security assessment
- [Budget & Rate Limiting](docs/budget-ratelimit.md) - Budget and rate limit design

## License

Private - Internal use only.
