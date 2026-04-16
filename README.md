# ADP — Agentic Developer Platform

Multi-tenant AI infrastructure for developer tools. Four components on a shared AWS platform.

## Modules

| Module | Path | What it does | Status |
|--------|------|-------------|--------|
| [Gateway](modules/gateway/) | `modules/gateway/` | Multi-tenant Bedrock proxy with Cognito auth, budgets, rate limiting, admin UI | Active |
| [Agent Factory](modules/agent-factory/) | `modules/agent-factory/` | Autonomous code agents (Claude SDK + Bedrock) triggered by GitHub issue labels | Active |
| [Agent Gateway](modules/agent-factory/gateway/) | `modules/agent-factory/gateway/` | Async agent delivery via Slack, WebSocket, CLI (API GW + SQS + KEDA) | Active |
| [MCP Gateway](modules/mcp-gateway/) | `modules/mcp-gateway/` | MCP server gateway for agent messaging and tool routing | In Progress |

## Architecture

```
                              Internet
                                 │
                    ┌────────────▼───────────┐
                    │       CloudFront       │
                    └────────────┬───────────┘
                                 │
                ┌────────────────┼────────────────┐
                │                │                │
     ┌──────────▼──────┐  ┌─────▼──────┐  ┌─────▼──────────┐
     │  S3 (Admin UI)  │  │ Internal   │  │ API Gateway WS │
     │  React+Tailwind │  │ ALB (EKS)  │  │ (Agent Gateway)│
     └─────────────────┘  └─────┬──────┘  └─────┬──────────┘
                                │                │
                    ┌───────────▼────────────────▼┐
                    │     Shared EKS Cluster      │
                    │         (adp-dev-eks)        │
                    │                              │
                    │  ┌─────────┐  ┌───────────┐ │
                    │  │ Gateway │  │ARC Runners│ │
                    │  │  Pods   │  │  (Agents) │ │
                    │  └────┬────┘  └─────┬─────┘ │
                    │       │       ┌─────▼─────┐ │
                    │       │       │KEDA Scaled│ │
                    │       │       │  Jobs     │ │
                    │       │       └─────┬─────┘ │
                    └───────┼─────────────┼───────┘
                            │             │
              ┌─────────────┼─────────────┼──────────────┐
              │             │             │              │
     ┌────────▼───┐  ┌─────▼────┐  ┌─────▼────┐  ┌─────▼────┐
     │    RDS     │  │  Redis   │  │ Bedrock  │  │   SQS    │
     │ PostgreSQL │  │(optional)│  │ (Claude) │  │ + DynamoDB│
     └────────────┘  └──────────┘  └──────────┘  └──────────┘
```

## Prerequisites

- AWS CLI v2 with admin access
- Terraform >= 1.5
- Docker
- kubectl + Helm v3
- Node.js >= 22
- Python >= 3.12
- GitHub CLI (`gh`)

## Deploy with Your AI Agent (Recommended)

The fastest way to deploy ADP is to let your AI coding agent handle it. Open this repo in any AI-powered editor (Kiro, Claude Code, Cursor, Copilot) and say:

> "Read AGENTS.md and deploy this platform"

The agent will:
1. Ask which modules you want (gateway, agent factory, or both)
2. Ask for your GitHub org name and configure the repo for your org
3. Run preflight checks and tell you what to install if anything is missing
4. Deploy the shared platform (VPC, EKS, ECR)
5. Deploy your chosen modules, verifying each step
6. Guide you through GitHub App setup (for Agent Factory)
7. Deploy the Agent Gateway (WebSocket API, SQS queues, KEDA)
8. Give you a final status summary with URLs and next steps

The agent handles everything autonomously — it only asks you when it genuinely needs input (AWS credentials, GitHub org name, GitHub App creation).

The deployment instructions live in three files for maximum tool compatibility:
- `AGENTS.md` — universal (any agent can read this)
- `CLAUDE.md` — Claude Code auto-reads this on startup
- `.kiro/steering/deployment.md` — Kiro auto-loads this into context

## Deploy with Scripts (Manual)

If you prefer to run things yourself, three scripts handle the full lifecycle:

```bash
# 1. Configure for your GitHub org (required for Agent Factory)
./platform/scripts/setup-org.sh <your-github-org>

# 2. Validate your environment
./platform/scripts/preflight-check.sh

# 3. Deploy everything (runs in AWS via CodeBuild — only needs AWS CLI)
./platform/scripts/deploy-all.sh
```

Options for `deploy-all.sh`:
- `--gateway-only` — deploy platform + gateway, skip agent factory
- `--agent-factory-only` — deploy platform + agent factory + agent gateway, skip gateway
- `--skip-frontend` — skip frontend build
- `--local` — run Terraform/Docker/npm locally instead of CodeBuild
- `--destroy` — tear down all infrastructure

## Deploy Step-by-Step

If you want full control over each phase:

### Step 1: Bootstrap Terraform State Backend

```bash
cd platform/scripts
export AWS_REGION=us-east-1
export ENVIRONMENT=dev
./bootstrap.sh
```

### Step 2: Deploy Shared Platform

```bash
cd platform/infra
terraform init -backend-config=../../environments/dev/backend.tfvars
terraform apply -var-file=../../environments/dev/platform.tfvars
```

### Step 3: Deploy Modules

Pick the modules you need. Each has its own Terraform and deployment steps.

#### Gateway (Bedrock Proxy)

Multi-tenant proxy for Amazon Bedrock with Cognito auth, cascading budgets, rate limiting, and an admin dashboard.

```bash
# Infrastructure (RDS, Redis, Cognito, CloudFront, S3, etc.)
cd modules/gateway/infra
terraform init -backend-config=../../../environments/dev/modules/gateway-backend.tfvars
terraform apply -var-file=../../../environments/dev/modules/gateway.tfvars

# Backend (Docker → ECR → EKS)
cd modules/gateway
docker build -t adp-gateway .
# Push to ECR, then:
kubectl apply -f k8s/ -n adp-gateway

# Frontend (React → S3 → CloudFront)
cd modules/gateway/frontend
npm ci && npm run build
aws s3 sync dist/ s3://<frontend-bucket>/ --delete
```

Full details: [modules/gateway/README.md](modules/gateway/README.md)

#### Agent Factory (Code Agents + Agent Gateway)

Autonomous AI agents that implement GitHub issues using Claude on Bedrock. Includes the Agent Gateway for async delivery via Slack, WebSocket, and CLI channels.

```bash
# Infrastructure (Runner IAM, ARC, Secrets Manager, beads, Agent Gateway: SQS + API GW + KEDA)
cd modules/agent-factory/infra
terraform init -backend-config=../../../environments/dev/modules/agent-factory-backend.tfvars
terraform apply -var-file=terraform.tfvars

# Agent Gateway (Docker image + KEDA ScaledJob)
cd modules/agent-factory/scripts
./deploy-gateway.sh

# Store GitHub App credentials
aws secretsmanager put-secret-value --secret-id adp/gh-app-dev-id --secret-string "<APP_ID>"
aws secretsmanager put-secret-value --secret-id adp/gh-app-dev-key --secret-string "$(cat key.pem)"

# Test: label any issue with "agent-developer"
gh issue edit <NUMBER> --add-label "agent-developer"
```

Full details: [modules/agent-factory/SETUP-GUIDE.md](modules/agent-factory/SETUP-GUIDE.md)

#### MCP Gateway

MCP server gateway for agent messaging and tool routing. In progress.

See: [modules/mcp-gateway/](modules/mcp-gateway/)

## Local Development (No AWS Required)

For the gateway, you can run locally with Docker Compose:

```bash
cd modules/gateway
docker compose up
# Backend at http://localhost:8080, Postgres at :5432, Redis at :6379
```

For the agent runtime:

```bash
cd modules/agent-factory/agent
npm install
npm run build
npm test
```

## CI/CD Workflows

All workflows live in `.github/workflows/`:

| Workflow | Trigger | Module |
|----------|---------|--------|
| `gateway-ci.yml` | PR to `modules/gateway/src/**` | Gateway — lint, test, Docker build |
| `gateway-deploy.yml` | Push to main (`modules/gateway/src/**`) | Gateway — ECR, EKS, S3, CloudFront |
| `gateway-infra.yml` | Push/PR to `modules/gateway/infra/**` | Gateway — Terraform plan/apply |
| `platform-infra-plan.yml` | Platform infra changes | Platform — Terraform plan |
| `platform-infra-apply.yml` | Platform infra changes | Platform — Terraform apply |
| `agent-developer.yml` | `agent-developer` label | Agent Factory — code implementation |
| `agent-architect.yml` | `agent-architect` label | Agent Factory — architecture design |
| `agent-pm.yml` | `agent-pm` label | Agent Factory — project management |
| `agent-reviewer.yml` | `agent-reviewer` label | Agent Factory — code review + merge |
| `agent-product.yml` | `agent-product` label | Agent Factory — requirements analysis |
| `agent-operations.yml` | `agent-operations` label | Agent Factory — infrastructure + deploy |
| `pr-review-trigger.yml` | PR from `agent/*` branch | Agent Factory — auto-triggers reviewer |
| `skill-agent.yml` | `skill-agent` label | Agent Factory — skill-driven agent |

## Directory Structure

```
adp/
├── platform/                    # Shared infrastructure
│   ├── infra/                   # Terraform (VPC, EKS, ECR, IAM)
│   ├── k8s/                     # Cluster-wide K8s resources
│   └── scripts/                 # deploy-all.sh, bootstrap.sh, setup-org.sh, preflight-check.sh
│
├── modules/
│   ├── gateway/                 # Bedrock Gateway
│   │   ├── src/                 # Python backend (FastAPI)
│   │   ├── frontend/            # React admin UI
│   │   ├── infra/               # Terraform (RDS, Cognito, CloudFront, etc.)
│   │   ├── k8s/                 # K8s manifests
│   │   ├── lambda/              # Lambda functions (authorizer, budget tracker)
│   │   ├── cloudwatch-agent/    # Log subscription + monitoring
│   │   ├── cli/                 # Claude Code auth CLI
│   │   ├── tests/               # pytest suite
│   │   └── docs/                # OpenAPI spec, schema docs
│   │
│   ├── agent-factory/           # Code Agents
│   │   ├── agent/               # TypeScript agent runtime (Claude SDK)
│   │   ├── gateway/             # Agent Gateway (async delivery)
│   │   │   ├── app/             # SQS consumer + persona loader
│   │   │   ├── lambdas/         # Ingest (classifier, channels) + Response (routers)
│   │   │   └── k8s/             # KEDA ScaledJob manifests
│   │   ├── rules/               # Agent personas, phases, templates
│   │   ├── infra/               # Terraform (runner IAM, ARC, secrets, beads, gateway)
│   │   ├── actions/             # GitHub composite actions
│   │   ├── client-workflows/    # Reusable workflow callers for other repos
│   │   ├── runner-infra/        # Reference: standalone runner setup
│   │   ├── docker/              # github-token-refresher
│   │   └── scripts/             # Build, deploy, deploy-gateway.sh
│   │
│   └── mcp-gateway/             # MCP Gateway
│       ├── docker/              # agent-mail MCP server
│       ├── scripts/             # Deploy scripts
│       └── *.md                 # Requirements, design docs
│
├── environments/                # Terraform var files (dev/staging/prod)
├── libs/                        # Shared libraries (Python, TypeScript)
├── .github/workflows/           # All CI/CD + agent workflows
├── AGENTS.md                    # Agent deployment playbook (universal)
├── CLAUDE.md                    # Agent deployment playbook (Claude Code)
└── .kiro/steering/              # Agent deployment playbook (Kiro)
```

## Documentation

| Doc | Location |
|-----|----------|
| Gateway README | [modules/gateway/README.md](modules/gateway/README.md) |
| Gateway OpenAPI Spec | [modules/gateway/docs/openapi.yaml](modules/gateway/docs/openapi.yaml) |
| Agent Factory Setup | [modules/agent-factory/SETUP-GUIDE.md](modules/agent-factory/SETUP-GUIDE.md) |
| Agent Factory README | [modules/agent-factory/README.md](modules/agent-factory/README.md) |
| Agent Gateway Routing | [modules/agent-factory/gateway/docs/intelligent-routing.md](modules/agent-factory/gateway/docs/intelligent-routing.md) |
| MCP Gateway Requirements | [modules/mcp-gateway/mcp_gateway_requirements.md](modules/mcp-gateway/mcp_gateway_requirements.md) |
| Deployment Playbook | [AGENTS.md](AGENTS.md) |

## License

Private — Internal use only.
