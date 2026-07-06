# ADP — Agentic Developer Platform

**ADP is the foundation for building and running AI agents across an enterprise.** Not one product — a base on which many agentic platforms get built (AI Operations, Cyber Threat, Agentic Analytics, …), with governance and security centralized and delivery owned locally by each team.

Concretely: you bring the agent's job, and ADP handles everything around it — model access, scaling, auth, tenant isolation, memory, tool access, audit, and cost controls. Adding a new agent is a five-file task; the platform provides the rest. It's the layer between "a clever prompt that works on your laptop" and "an agent a whole org can safely use against real systems."

## What you get out of the box

- **Governed model access** — a multi-tenant Amazon Bedrock proxy with per-org/team/user budgets, rate limits, and full audit. Point Claude Code, Cursor, or any OpenAI-compatible client at one endpoint.
- **A team of autonomous coding agents** — mention an agent on a GitHub issue or PR; a pod spins up, does the work, and opens a PR. No runner babysitting. The personas cover the full AI development life cycle (AIDLC):
  - **@agent-product** — gathers requirements, writes user stories + acceptance criteria
  - **@agent-pm** — orchestrates the workflow, decomposes work, coordinates the board
  - **@agent-architect** — designs systems, defines interfaces, produces design docs
  - **@agent-developer** — writes production code + tests, opens the PR
  - **@agent-reviewer** — the quality gate: reviews for correctness/security, blocks on real issues
  - **@agent-operations** — deploys, monitors, and maintains infrastructure

  Domains add their own (e.g. **@malware-analysis-agent** in the cyber pack), and new personas are a five-file declaration — no platform changes.
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

## Where this is heading: one control plane, many domain planes

> **Vision / north star — not yet built.** This describes the direction ADP is being built toward, not current capability.

**The driver: enterprise-wide AI transformation needs a common foundation.** When every team races to adopt agents on its own, you get N incompatible stacks, N security postures, and N budget blind spots. The answer isn't to centralize all the work — that kills delivery speed — it's a **shared base each domain can customize, tailor, and localize, while governance and security stay centralized.**

ADP is that base. A single org runs **one control plane** and **multiple domain planes** — each domain plane its own ADP instance specialized for a problem space:

- **AI Operations** — running, monitoring, and remediating AI/ML systems
- **Cyber Threat** — malware analysis and threat research (the first domain today)
- **Agentic Analytics** — agent-driven data pipelines and insight generation
- *…and others, each a peer domain plane*

```
                    ┌──────────────────────────────┐
                    │         Control Plane        │
                    │  org-wide governance: policy, │
                    │  identity, budgets, audit,    │
                    │  model access, provenance     │
                    └───────────────┬──────────────┘
                          centralized guardrails
            ┌────────────────┬──────┴───────┬────────────────┐
            ▼                ▼              ▼                ▼
   ┌────────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────┐
   │ AI Operations  │ │ Cyber Threat │ │  Agentic     │ │   …      │
   │  domain plane  │ │ domain plane │ │  Analytics   │ │ (more)   │
   │ (ADP instance) │ │(ADP instance)│ │ domain plane │ │          │
   └────────────────┘ └──────────────┘ └──────────────┘ └──────────┘
      each team tailors + localizes its own agents, tools, delivery
```

**Two planes — governance *and* speed, not one at the expense of the other:**

- **Control plane → centralized governance & security.** One place for policy, identity, budgets, audit, model access, and provenance. Security and finance get a single pane of glass; guardrails are set once and inherited by every domain.
- **Domain planes → local autonomy & speed.** Each team gets the same foundation but **customizes and localizes** it — its own agents, tools, domain infra, and release cadence — and ships without waiting on a central queue, while still operating inside the control plane's guardrails.

This is a **platform-of-platforms** shape — established patterns enterprises already trust, applied to agents. The control-plane / domain-plane split mirrors **AWS Control Tower** (org-wide guardrails over many autonomous accounts); the extensible, domain-customizable foundation mirrors **Spotify Backstage** (one base many teams extend). ADP is following those patterns for the agentic era — *we're building toward this, not claiming parity with those mature products.*

**Open by design — for maximum flexibility:**

- **Open standards.** Agents reach the world through open, vendor-neutral interfaces — MCP for tools/context, standard GitHub webhooks, OpenAI-compatible model APIs — so a domain team can swap a backend or bring its own without re-platforming. No proprietary lock-in at the boundaries.
- **Open source (coming).** The foundation is moving to an open-source license so any team can run, inspect, extend, and contribute back — maximum flexibility for domains to customize, with a shared upstream so improvements compound across the org instead of fragmenting into forks.

That's the enterprise-transformation payoff: a common, open foundation that accelerates every team, with org-wide control and security assisting rather than blocking. Today ADP ships the **domain-plane substrate** (this repo), with cyber as the first domain; the cross-instance control plane and the open-source release are the next major build-outs.

## Modules

Where the code lives and how mature each piece is (the capabilities are described in *What you get out of the box* above):

| Module | Path | Status |
|--------|------|--------|
| [Gateway](modules/gateway/) | `modules/gateway/` | Active |
| [Agent Factory](modules/agent-factory/) | `modules/agent-factory/` | Active |
| [Agent Context](modules/agent-context/) | `modules/agent-context/` | Active |
| [Domain Apps](modules/domain-apps/) | `modules/domain-apps/` | Active (cyber) |
| [MCP Hub](modules/harness/mcp-hub/) | `modules/harness/mcp-hub/` | In Progress |
| [User Services](modules/user-services/) | `modules/user-services/` | Design |

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

Humans and services arrive through two front doors — the **admin/chat UI** (CloudFront → S3 + internal ALB) and **API Gateway** (GitHub webhooks + WebSocket chat). Everything runs on one shared EKS cluster, where the three core services and any domain apps sit side by side.

```
   Humans · GitHub · Slack · CLI · OpenAI-compatible clients
                          │
        ┌─────────────────┼────────────────────────┐
        │                 │                         │
  ┌─────▼──────┐   ┌───────▼──────┐        ┌─────────▼─────────┐
  │ CloudFront │   │ Internal ALB │        │   API Gateway     │
  │  → S3 UI   │   │   (→ EKS)    │        │ webhook + WS chat │
  └─────┬──────┘   └───────┬──────┘        └─────────┬─────────┘
        │                  │            (HMAC/auth → Lambda → SQS)
        └──────────────────┼─────────────────────────┘
                           │
   ┌───────────────────────▼──────────────────────────────────────┐
   │                 Shared EKS Cluster (adp-dev-eks)              │
   │                                                              │
   │  ┌──────────────┐  ┌───────────────┐  ┌───────────────────┐ │
   │  │   Gateway    │  │ Agent Factory │  │   Agent Context   │ │
   │  │ Bedrock proxy│  │ KEDA ScaledJob│  │   MCP endpoint    │ │
   │  │ + admin API  │  │ → agent-worker│  │ (semantic + code  │ │
   │  │              │  │   pods        │  │  search, wiki,    │ │
   │  └──────┬───────┘  └──────┬────────┘  │  memory)          │ │
   │         │                 │           └─────────┬─────────┘ │
   │  ┌──────┴───────┐         │           ┌─────────▼─────────┐ │
   │  │ Domain apps  │◄────────┘           │ OpenViking ·      │ │
   │  │ cyber: malware│  agents reach      │ Sourcebot ·       │ │
   │  │ workers + ARC │  Context + tools   │ DeepWiki ·        │ │
   │  │ runners       │  via the harness   │ LiteLLM proxy     │ │
   │  └──────┬───────┘                     └───────────────────┘ │
   └─────────┼──────────────────────────────────────────────────┘
             │
   ┌─────────┼─────────┬──────────┬───────────┬───────────────┐
   │         │         │          │           │               │
┌──▼───┐ ┌───▼───┐ ┌───▼────┐ ┌───▼─────┐ ┌───▼────┐  ┌────────▼────┐
│ RDS  │ │ Redis │ │Bedrock │ │  SQS +  │ │  S3    │  │ Secrets /   │
│ (PG) │ │(rate  │ │(Claude,│ │ DynamoDB│ │(arti-  │  │ Cognito     │
│      │ │ limit)│ │ Titan) │ │ queues, │ │ facts) │  │ (authn/z)   │
└──────┘ └───────┘ └────────┘ │ index)  │ └────────┘  └─────────────┘
                              └─────────┘
```

Three core services run as peers on the cluster: **Gateway** (governed Bedrock access + admin API), **Agent Factory** (the agent runtime, fed by the webhook / conversational / ARC front doors), and **Agent Context** (code intelligence behind a single MCP endpoint, fronting OpenViking, Sourcebot, DeepWiki, and a LiteLLM proxy). **Domain apps** (e.g. cyber/malware) bring their own workers and reach the other services only through the harness.

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

**There is no upfront GitHub setup.** Everything keys off the AWS account your active profile resolves to. For the agent path, GitHub is wired at the **end** (UI flow: Settings → Connections → "Set up GitHub App"; or CLI fallback `register-github-app.sh`); gateway-only needs no GitHub at all.

The phases at a glance (each step idempotent and re-runnable):

| Phase | What it does | Script | Needed for |
|------:|--------------|--------|-----------|
| 1 | Terraform state backend (S3 + DynamoDB) | `platform/scripts/bootstrap.sh` | All |
| 2 | Environment / preflight validation | `platform/scripts/preflight-check.sh` | All |
| 3 | Platform infra (VPC, EKS, ECR, IAM) | terraform / `deploy-all.sh` | All |
| 4 | Gateway infra (RDS, Redis, Cognito, CloudFront, S3) | terraform / `deploy-all.sh` | Gateway |
| 5 | Gateway backend on EKS (image → ECR → pods) | terraform / `deploy-all.sh` | Gateway |
| 6 | Frontend (React → S3 → CloudFront) + CFN template upload | `modules/gateway/scripts/deploy-frontend.sh` | Gateway |
| 6b | Gateway second pass — wire ALB (MOCK API GW → real routes) | `wire-gateway-alb.sh --apply` | Gateway |
| 6c | Broker Lambda code (real GitHub-login handler) | `modules/gateway/scripts/deploy-broker.sh` | Login |
| 6d | Seed the first admin (org/user/role) | `modules/gateway/scripts/bootstrap-admin.sh` | Login |
| 7 | Webhook agent stack + agent-runtime image (warm pool + image-prepull) | `webhook-ingress/scripts/deploy-webhook-ingress.sh` | Agents |
| 8 | Bedrock model access (⚠️ human — console *Subscribe*, no CLI) | *(AWS console)* | Agents |
| 9 | Create + wire the GitHub App (⚠️ human browser step) | **UI:** Settings → Connections → "Set up GitHub App" (as `platform_admin`). **CLI fallback:** `register-github-app.sh <org>` | Agents |
| 10 | End-to-end smoke test | *(curl + `@agent-developer` task)* | Verify |

Phase numbering matches the canonical sequence in [`deploy-quickstart.md`](docs/adp-platform-deployment/deploy-quickstart.md). The **ADP-managed** (pipeline) equivalent of these same phases — run as GitHub Actions workflows (`platform-infra-apply.yml`, `gateway-deploy.yml`, `webhook-ingress-deploy.yml`, …) instead of local scripts — is captured in the per-deploy-instance runbook (e.g. issue #1320). Same phases, two execution mechanisms; don't mix them in one run.

**`deploy-all.sh` chains Phases 1–6 (incl. the 6b ALB pass) but NOT 6c, 6d, 7, 8, 9.** Why: Terraform ships *placeholders* for things a push-triggered CI workflow normally publishes (the MOCK API Gateway body, a 503 broker Lambda stub, `:latest` image refs, the webhook Lambda zip). A fresh manual deploy fires none of those, so `deploy-all.sh` alone leaves you without working login, a first admin, or the agent path — run the 6c/6d/7/9 scripts and enable Bedrock access (Phase 8) manually (deploy-quickstart.md sequences them; don't skip them).

`deploy-all.sh` flags:

| Flag | Effect |
|------|--------|
| `--gateway-only` | platform + gateway only (no GitHub needed) |
| `--agent-context-only` | platform + Agent Context (code intelligence) |
| `--skip-frontend` | skip the React frontend build |
| `--local` | build images with local Docker instead of CodeBuild |
| `--destroy` | tear down (reverse order: agent-context → agent-factory → gateway → platform) |

### Deploy with your AI agent

Open the repo in any AI editor (Claude Code, Kiro, Cursor) and say *"Read the deploy-with-agent guide and deploy this platform."* The agent confirms your target AWS account, then executes the phases, verifying each before moving on, and only stops for genuine input (AWS account choice, the GitHub App browser steps).

→ **[`docs/adp-platform-deployment/deploy-with-agent.md`](docs/adp-platform-deployment/deploy-with-agent.md)** is the canonical agent-deploy guide. `AGENTS.md`, `CLAUDE.md`, and `.kiro/steering/deployment.md` all point to it, so there's one source of truth.

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

This project is licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the full text.
