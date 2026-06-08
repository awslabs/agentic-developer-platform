# ADP — Agentic Developer Platform

**ADP is the infrastructure for running AI agents.** You bring the agent's job; ADP handles everything around it — model access, scaling, auth, tenant isolation, memory, tool access, audit, and cost controls. Adding a new agent is a five-file task; the platform provides the rest.

It's the layer between "a clever prompt that works on your laptop" and "an agent a whole org can safely use against real systems."

## What you get out of the box

- **Governed model access** — a multi-tenant Amazon Bedrock proxy with per-org/team/user budgets, rate limits, and full audit. Point Claude Code, Cursor, or any OpenAI-compatible client at one endpoint.
- **Autonomous code agents** — mention an agent on a GitHub issue or PR; a pod spins up, does the work, and opens a PR. No runner babysitting.
- **Code intelligence** — one MCP endpoint giving agents semantic search, code search, wikis, and persistent memory across your codebases.
- **A shared harness** — tool routing, jobs, events, artifacts, and human-in-the-loop approvals, so every agent gets the same plumbing instead of reinventing it. *(In progress — see `ARCHITECTURE.md`.)*

## Where it fits

- Give a whole org **safe, metered access to LLMs** without each team wiring its own Bedrock setup.
- Run **autonomous agents on your repos** — implementation, review, ops — triggered from GitHub or chat.
- Build **your own domain agents** (security research, AI operations, data) on a substrate that already handles identity, scaling, and audit.
- Ship **per-user products** (vault, knowledge repo, chief-of-staff) that act on a single person's behalf.

## The model in one line

**Apps declare. Harness operates. Platform runs. User services are owned by the user.**

Agents are the consumers; humans and services invoke them through a separate inbound surface. To add capability, you write declarations — tools, jobs, events, skills, agents — and the harness handles the rest. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full mental model.

## Modules

| Module | Path | What it does | Status |
|--------|------|-------------|--------|
| [Gateway](modules/gateway/) | `modules/gateway/` | Multi-tenant Bedrock proxy with Cognito auth, budgets, rate limiting, admin UI | Active |
| [Agent Factory](modules/agent-factory/) | `modules/agent-factory/` | Autonomous agents (Claude SDK + Bedrock) with three execution models — GitHub webhook, Conversational Gateway, and self-hosted runners | Active |
| [Agent Context](modules/agent-context/) | `modules/agent-context/` | Code intelligence: semantic search, code search, wikis, and memory behind one MCP endpoint (fronts OpenViking, Sourcebot, DeepWiki, LiteLLM) | Active |
| [Domain Apps](modules/domain-apps/) | `modules/domain-apps/` | Self-contained capability packs built on the substrate — cyber/malware analysis is the first | Active (cyber) |
| [MCP Hub](modules/harness/mcp-hub/) | `modules/harness/mcp-hub/` | MCP tools surface of the harness (part of the harness; see `ARCHITECTURE.md`) | In Progress |
| [User Services](modules/user-services/) | `modules/user-services/` | Per-user products the user owns — vault (credentials), knowledge repo, bespoke agents, chief-of-staff | Design |

### Agent Factory — three execution models

Autonomous agents share one runtime but can be summoned through different front doors, and can also drive deterministic pipelines:

- **GitHub (webhook-ingress)** — `@mention` or label an issue/PR → `API Gateway → Lambda (HMAC + tenant lookup) → SQS FIFO → KEDA → agent-worker` spins up, does the work, and opens a PR. No self-hosted runners needed.
- **Conversational Gateway** — talk to agents from **Slack, a web chat UI, or CLI**; an Ingest Lambda uses a fast Bedrock classifier to route each message to the right execution path (`WebSocket/SQS → KEDA → response Lambdas`).
- **Self-hosted runners (ARC)** — deterministic GitHub Actions pipelines on EKS that an agent can **trigger and monitor**. The right tool when you need a known, auditable, repeatable sequence (e.g. complex multi-stage deployments) rather than open-ended agent reasoning. Setup: [`modules/agent-factory/SETUP-GUIDE.md`](modules/agent-factory/SETUP-GUIDE.md).

### Build your own: domain apps

The core modules are the substrate — the leverage is building **domain apps** on top. A domain app is a self-contained pack for a problem space, plugged into the shared harness. You write the *declarations* — agents, tools, jobs, events, skills, schemas — and the platform provides model access, scaling, identity, audit, and memory underneath.

**Working example — the cyber domain** (`modules/domain-apps/cyber/`): a **malware-analysis agent** that takes a sample (S3 pointer + a GitHub issue), runs a deterministic 7-stage pipeline, and posts a structured report back to the issue. It shows the patterns a serious domain needs:
- **reasoning/byte-handling isolation** — the reasoning agent never touches sample bytes; byte-handling workers have no model or internet access;
- heavy stages dispatched to dedicated **worker queues**;
- its own **domain infra** (sandbox cluster, workers) alongside the shared platform.

**Build the next one** — the same shape applies to any domain. You add an `apps/<domain>/` pack (`agents/`, `tools/`, `jobs/`, `events/`, `skills/`, `schemas/`, optional `frontend/` + `infra/`). Apps are peers: they talk only through the harness (events, tools, context), never by importing each other — so a new domain ships without touching the platform or other apps.

**Planned domains** — **AI Operations** (agents that run, monitor, and remediate AI/ML systems) and **Data Platforms** (agent-driven data pipelines and governance), each shipping as a new pack on the same substrate.

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
     ┌──────────▼──────┐  ┌─────▼──────┐  ┌─────▼──────────────┐
     │  S3 (Admin UI)  │  │ Internal   │  │ API Gateway        │
     │  React+Tailwind │  │ ALB (EKS)  │  │ (webhook + WS chat)│
     └─────────────────┘  └─────┬──────┘  └─────┬──────────────┘
                                │                │ (→ Lambda → SQS)
                    ┌───────────▼────────────────▼┐
                    │     Shared EKS Cluster      │
                    │        (adp-dev-eks)         │
                    │                              │
                    │  ┌─────────┐  ┌───────────┐ │
                    │  │ Gateway │  │KEDA Scaled│ │
                    │  │  Pods   │  │   Jobs    │ │
                    │  └────┬────┘  └─────┬─────┘ │
                    │       │       ┌─────▼─────┐ │
                    │       │       │  Agent    │ │
                    │       │       │  Workers  │ │   (+ ARC runners
                    │       │       │  + Context│ │    for pipelines)
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

## Lightweight Install (Agents Only)

Want to run the ADP code agents on your own AWS account without deploying the full platform? If you already have an EKS cluster with ARC runners, you can be up and running in 15 minutes:

```bash
./platform/scripts/lightweight-setup.sh
```

See **[docs/lightweight-install.md](docs/lightweight-install.md)** for the full guide, prerequisites, and troubleshooting.

## Prerequisites

- AWS CLI v2 with admin access
- Terraform >= 1.14
- Docker
- kubectl + Helm v3
- Node.js >= 22
- Python >= 3.12
- GitHub CLI (`gh`)

> **Authoritative deploy guide:** [`docs/adp-platform-deployment/deploy-quickstart.md`](docs/adp-platform-deployment/deploy-quickstart.md) is the verified, phase-by-phase procedure maintained against real end-to-end runs. Start there; the sections below are the orientation.

## Deploying

> **The authoritative, verified procedure is [`docs/adp-platform-deployment/deploy-quickstart.md`](docs/adp-platform-deployment/deploy-quickstart.md)** — maintained against real end-to-end runs. Follow it for the exact phase sequence, commands, verification, and gotchas. The summary below is orientation only; don't deploy from it.

**There is no upfront GitHub setup.** Everything keys off the AWS account your active profile resolves to. For the agent path, GitHub is wired at the **end** (`register-github-app.sh`); gateway-only needs no GitHub at all.

A deploy is a sequence of idempotent, stage-by-stage scripts:

```bash
export AWS_PROFILE=<profile> AWS_REGION=us-east-1     # the account everything keys off
aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output table  # confirm target

./platform/scripts/bootstrap.sh                       # 1  Terraform state backend
./platform/scripts/preflight-check.sh                 # 2  environment validation
# 3–6  platform infra + gateway infra + backend + frontend (terraform applies + image/frontend builds)
./platform/scripts/wire-gateway-alb.sh --apply        # 6b gateway second pass (MOCK API GW → real ALB routes)
./modules/gateway/scripts/deploy-broker.sh --env dev  # 6c GitHub-login broker Lambda code
./modules/gateway/scripts/bootstrap-admin.sh --env dev # 6d seed the first admin (REQUIRED for login)
# Agent path (optional):
./modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh --env dev   # 7  webhook stack + agent-runtime image
./modules/agent-factory/webhook-ingress/scripts/register-github-app.sh <org> --env dev # 8  create + wire the GitHub App
```

**Why the extra scripts beyond `deploy-all.sh`:** Terraform ships *placeholders* for things a push-triggered CI workflow normally publishes (the MOCK API Gateway body, a 503 broker Lambda stub, `:latest` image refs, the webhook Lambda zip). A fresh manual deploy fires none of those workflows, so `deploy-all.sh` alone leaves you without working login, a first admin, or the agent path. The stage-by-stage scripts above (`wire-gateway-alb.sh --apply`, `deploy-broker.sh`, `bootstrap-admin.sh`, `deploy-webhook-ingress.sh`, `register-github-app.sh`) are the manual equivalents — deploy-quickstart.md sequences them. **Don't skip them.**

`deploy-all.sh` chains the infra phases (1–6, incl. the ALB pass) but **not** the broker, first-admin bootstrap, or webhook/agent path:

| Flag | Effect |
|------|--------|
| `--gateway-only` | platform + gateway only (no GitHub needed) |
| `--agent-context-only` | platform + Agent Context (code intelligence) |
| `--skip-frontend` | skip the React frontend build |
| `--local` | build images with local Docker instead of CodeBuild |
| `--destroy` | tear down (reverse order: agent-context → agent-factory → gateway → platform) |

### Deploy with your AI agent

Open the repo in any AI editor (Claude Code, Kiro, Cursor) and say *"Read CLAUDE.md and deploy this platform."* The agent confirms your target AWS account, then executes the phases in deploy-quickstart.md, verifying each before moving on, and only stops for genuine input (AWS account choice, the GitHub App browser steps). Instructions live in `AGENTS.md` (universal), `CLAUDE.md` (Claude Code auto-reads on startup), and `.kiro/steering/deployment.md` (Kiro).

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
| `gateway-deploy.yml` | Push to main (`modules/gateway/src/**`, `frontend/**`) | Gateway — ECR, EKS, S3, CloudFront |
| `gateway-infra-plan.yml` / `-apply.yml` / `-destroy.yml` | Gateway infra changes / dispatch | Gateway — Terraform plan/apply/destroy |
| `platform-infra-plan.yml` / `-apply.yml` | Platform infra changes | Platform — Terraform plan/apply |
| `webhook-ingress-ci.yml` / `-deploy.yml` / `-destroy.yml` | Webhook-ingress changes / dispatch | Agent Factory — GitHub webhook agent stack |
| `agent-gateway-deploy.yml` | Conversational Gateway changes | Agent Factory — Conversational Gateway delivery |
| `agent-worker-image.yml` | Agent runtime changes | Agent Factory — build `adp-agent-runtime` image |
| `agent-factory-infra-{plan,apply,destroy}.yml` | Agent Factory infra changes / dispatch | Agent Factory — Terraform (incl. ARC runners) |
| `agent-context-infra-{plan,apply,destroy}.yml`, `agent-context-images-build.yml`, `agent-context-ingest.yml` | Agent Context changes / dispatch | Agent Context — infra, images, ingestion |
| `agent-developer.yml`, `agent-reviewer.yml`, `agent-architect.yml`, `agent-pm.yml`, `agent-product.yml`, `agent-operations.yml`, `agent-pt-superpower.yml` | Issue/PR mention or label | Agent Factory — agent personas |
| `malware-analysis-agent.yml` | `malware-analysis-agent` label | Domain Apps (cyber) — 7-stage malware analysis |
| `cyber-infra-{plan,apply}.yml` | Cyber domain infra changes | Domain Apps (cyber) — Terraform |

(Run `ls .github/workflows/` for the complete, current set — agent triggers are moving from labels toward `@mention`-in-comments.)

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
│   ├── agent-factory/           # Autonomous agents (three execution models)
│   │   ├── agent/               # TypeScript agent runtime (Claude SDK)
│   │   ├── agent-worker-image/  # Container image for the agent worker pod
│   │   ├── webhook-ingress/     # GitHub webhook stack (API GW → Lambda → SQS → KEDA)
│   │   ├── gateway/             # Conversational Gateway (Slack / WebSocket / CLI delivery)
│   │   │   ├── app/             # SQS consumer + persona loader
│   │   │   ├── lambdas/         # Ingest (classifier, channels) + Response (routers)
│   │   │   └── k8s/             # KEDA ScaledJob manifests
│   │   ├── rules/               # Agent personas, phases, templates
│   │   ├── infra/               # Terraform (runner IAM, ARC, secrets, beads, gateway)
│   │   ├── runner-infra/        # ARC self-hosted runners (deterministic pipelines)
│   │   ├── actions/             # GitHub composite actions
│   │   ├── client-workflows/    # Reusable workflow callers for other repos
│   │   ├── docker/              # github-token-refresher
│   │   └── scripts/             # Build + deploy scripts
│   │
│   ├── agent-context/           # Code intelligence (one MCP endpoint)
│   │   ├── infra/               # Terraform (OpenViking, Sourcebot, DeepWiki, LiteLLM)
│   │   └── k8s/                 # MCP server + backend manifests
│   │
│   ├── domain-apps/             # Domain capability packs
│   │   └── cyber/               # Cyber domain — 7-stage malware-analysis agent
│   │       ├── agent/           # Personas (malware-analysis-agent, ...)
│   │       ├── workers/         # Byte-handling workers (no model / no internet)
│   │       ├── infra/ k8s/      # Domain-specific infra + manifests
│   │       └── scripts/         # Domain deploy scripts
│   │
│   ├── harness/                # Harness — outbound surface agents use
│   │   ├── contracts/          # Versioned schemas (tool, job, event, ...)
│   │   └── mcp-hub/            # MCP tools surface (in progress)
│   │
│   └── user-services/          # Per-user products (vault, knowledge repo, ...) — design
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
| **Architecture (mental model)** | [ARCHITECTURE.md](ARCHITECTURE.md) — the four categories, the two skins, where new work goes |
| **Deploy Quick Start (authoritative)** | [docs/adp-platform-deployment/deploy-quickstart.md](docs/adp-platform-deployment/deploy-quickstart.md) — verified phase-by-phase procedure |
| Self-Managed Deploy (full reference) | [docs/adp-platform-deployment/self-managed-deploy.md](docs/adp-platform-deployment/self-managed-deploy.md) |
| Gateway README | [modules/gateway/README.md](modules/gateway/README.md) |
| Gateway OpenAPI Spec | [modules/gateway/docs/openapi.yaml](modules/gateway/docs/openapi.yaml) |
| Agent Factory README | [modules/agent-factory/README.md](modules/agent-factory/README.md) |
| Agent Factory Setup (ARC runners) | [modules/agent-factory/SETUP-GUIDE.md](modules/agent-factory/SETUP-GUIDE.md) |
| Conversational Gateway Routing | [modules/agent-factory/gateway/docs/intelligent-routing.md](modules/agent-factory/gateway/docs/intelligent-routing.md) |
| Agent Context README | [modules/agent-context/README.md](modules/agent-context/README.md) |
| Cyber Domain (malware agent) | [modules/domain-apps/cyber/](modules/domain-apps/cyber/) |
| MCP Hub Requirements | [modules/harness/mcp-hub/mcp_gateway_requirements.md](modules/harness/mcp-hub/mcp_gateway_requirements.md) |
| Agent Coding Guidelines | [docs/agent-coding-guidelines.md](docs/agent-coding-guidelines.md) — universal behavioral rules every agent writing code must follow |
| Deployment Playbook | [AGENTS.md](AGENTS.md) |

## License

Private — Internal use only.
