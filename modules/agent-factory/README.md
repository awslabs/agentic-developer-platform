# Agent Factory

Autonomous code agents powered by Claude SDK and Amazon Bedrock, orchestrated via GitHub Actions on self-hosted EKS runners.

The hosted agents in this module run on the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk) (`@anthropic-ai/claude-agent-sdk`).

## What It Does

Agent Factory provides a team of AI agent personas that work on GitHub issues autonomously. Label an issue with `agent-developer`, and within minutes a pod spins up on EKS, clones the repo, analyzes the issue, implements changes, and opens a pull request. A reviewer agent can then automatically review and merge it.

The system supports two trigger paths:
1. **GitHub Actions** — label an issue, workflow dispatches to ARC runners on EKS
2. **Webhook Ingress** — multi-tenant hosted path where GitHub webhooks flow through API Gateway → Lambda → SQS FIFO → KEDA ScaledJob

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         TRIGGER PATHS                                        │
│                                                                             │
│  Path 1: GitHub Actions (single-tenant, self-hosted)                        │
│  ┌──────────────────────────────────────────────────────────────────┐       │
│  │ Issue labeled ──► GitHub Actions ──► ARC Runner Scale Set (EKS)  │       │
│  └──────────────────────────────────────────────────────────────────┘       │
│                                                                             │
│  Path 2: Webhook Ingress (multi-tenant, hosted)                             │
│  ┌──────────────────────────────────────────────────────────────────┐       │
│  │ GitHub webhook ──► API GW v2 ──► Lambda (HMAC + tenant lookup)   │       │
│  │                       │                    │                      │       │
│  │                    WAF rate-limit          SQS FIFO               │       │
│  │                                              │                    │       │
│  │                                           KEDA ScaledJob (pod)    │       │
│  └──────────────────────────────────────────────────────────────────┘       │
│                                                                             │
│  Path 3: WebSocket (real-time interactive)                                  │
│  ┌──────────────────────────────────────────────────────────────────┐       │
│  │ Client WS ──► API GW WebSocket ──► Ingest Lambda ──► SQS FIFO   │       │
│  │ Client ◄── Response Lambda ◄── SQS Response ◄── Agent Pod        │       │
│  └──────────────────────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Shared EKS Cluster (adp-dev-eks-cluster)                  │
│                                                                             │
│  ┌─────────────────┐    ┌────────────────────────────────────────────┐      │
│  │  arc-systems ns  │    │  arc-runners ns                            │      │
│  │                 │    │                                            │      │
│  │  ARC Controller │    │  Runner Pods (scale 0 → N on demand)      │      │
│  │  (watches for   │    │                                            │      │
│  │   workflow jobs) │    │  Each pod:                                 │      │
│  │                 │    │  ├── Clones target repo                    │      │
│  └─────────────────┘    │  ├── Loads agent runtime (TypeScript)      │      │
│                         │  ├── Calls Claude via Bedrock (IRSA)       │      │
│                         │  ├── Reads GitHub App token from           │      │
│                         │  │   Secrets Manager                       │      │
│                         │  ├── Implements changes                    │      │
│                         │  └── Creates PR via GitHub API             │      │
│                         └────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
           ┌──────────────┐ ┌─────────────┐ ┌──────────────┐
           │   Bedrock    │ │   Secrets   │ │   DynamoDB   │
           │  (Claude)    │ │   Manager   │ │  + S3        │
           │              │ │  (GH App    │ │  (Beads      │
           │  via IRSA    │ │   creds)    │ │   state)     │
           └──────────────┘ └─────────────┘ └──────────────┘
```

## Agent Personas

| Persona | Trigger Label | What It Does |
|---------|---------------|--------------|
| `@agent-developer` | `agent-developer` | Code implementation, unit tests, PR creation |
| `@agent-architect` | `agent-architect` | Architecture design, technical decisions, ADRs |
| `@agent-pm` | `agent-pm` | Issue decomposition, sub-issue creation, project planning |
| `@agent-reviewer` | `agent-reviewer` | Code review, test validation, PR approval/merge |
| `@agent-product` | `agent-product` | Requirements analysis, user stories, acceptance criteria |
| `@agent-operations` | `agent-operations` | Infrastructure, deployment, monitoring, runbooks |

Each persona uses a dedicated GitHub App for rate limit isolation (5,000 requests/hour per app). Three apps cover all six personas:
- DEV app → developer, architect
- PM app → pm, product
- OPS app → reviewer, operations

## Key Components

### Agent Runtime (`agent/`)

TypeScript application that orchestrates the agent's work:

```
agent/src/
├── components/       # Core: orchestrator, planner, code generator, state machine
├── services/         # Agent service, approval workflows, error recovery
├── types/            # TypeScript interfaces
└── utils/            # GitHub API helpers, resilient queries
```

The runtime: reads the issue → generates a plan (Claude via Bedrock) → executes step-by-step → commits → creates PR → optionally triggers reviewer.

### Rules (`rules/`)

Markdown instructions that shape agent behavior:

```
rules/
├── personas/         # Per-agent role definitions (developer.md, architect.md, etc.)
├── phases/           # SDLC phase guides (inception, construction, operations)
├── templates/        # Output templates (PR descriptions, issue decomposition)
└── workflows/        # Multi-step workflow definitions
```

### Webhook Ingress (`webhook-ingress/`)

The multi-tenant hosted trigger path. Receives GitHub webhooks, validates them, resolves tenants, and queues work for agent processing:

```
GitHub ──POST /github──► API Gateway HTTP v2 ──► Lambda (HMAC + tenant lookup)
                              │                        │
                              │ WAF (1000 req/5min/IP) ├──► DynamoDB (tenant-registry,
                              │                        │              events, rate-limits)
                              │                        │
                              │                        └──► SQS FIFO (agent-submit)
                                                                │
                                                                ├──► KEDA ScaledJob (agent pod)
                                                                └──► DLQ (after 3 failures)
```

| Resource | Name Pattern | Purpose |
|----------|-------------|---------|
| HTTP API v2 | `adp-<env>-webhook-ingress` | Webhook endpoint |
| Lambda | `adp-<env>-github-webhook` | HMAC signature validation, tenant lookup, event normalization |
| SQS FIFO | `adp-<env>-agent-submit.fifo` | Agent work queue. Ordered per-tenant via `MessageGroupId`. Content-based dedup. |
| SQS DLQ | `adp-<env>-agent-submit-dlq.fifo` | Failed message inspection |
| DynamoDB | `adp-<env>-tenant-registry` | GitHub App installation → tenant mapping |
| DynamoDB | `adp-<env>-webhook-events` | Audit log of all webhook events |
| DynamoDB | `adp-<env>-rate-limits` | Per-tenant rate limiting |
| WAF | Attached to API GW | 1000 req/5min per IP — abuse protection before Lambda executes |

Design choices: HTTP API v2 (71% cheaper than REST v1), FIFO queue (ordered per-tenant), content-based dedup (handles GitHub retries), 2-hour visibility timeout (matches max agent run time), separate from Bedrock Gateway API (different auth model, different blast radius).

### Agent Gateway (`gateway/`)

The real-time delivery pipeline for interactive agent sessions (chat, streaming):

| Component | Path | Role |
|-----------|------|------|
| SQS Consumer | `gateway/app/sqs_consumer.py` | Pulls tasks from SQS, dispatches to agent runtime |
| Ingest Lambda | `gateway/lambdas/ingest/` | Receives WebSocket messages, routes to SQS |
| Response Lambda | `gateway/lambdas/response/` | Streams agent responses back via WebSocket |
| KEDA ScaledJob | `gateway/k8s/keda-scaledjob.yaml` | Scales worker pods based on queue depth |
| Persona routing | `gateway/app/personas/` | Maps incoming messages to the correct agent persona |

### Infrastructure (`infra/`)

Terraform modules for the ARC runner path:

| Module | What It Creates |
|--------|----------------|
| `runner-iam/` | IRSA role with Bedrock, Secrets Manager, IAM, KMS, S3 permissions |
| `arc-runner/` | ARC controller + runner scale set (Helm releases) |
| `secrets/` | Secrets Manager entries for GitHub App credentials |
| `beads-state/` | DynamoDB table + S3 bucket for issue tracking state |
| `sqs/` | SQS FIFO queues for agent task delivery |
| `api-gateway-ws/` | WebSocket API Gateway for real-time sessions |
| `lambda-gateway/` | Ingest + response Lambdas |
| `dynamodb-sessions/` | Session state for WebSocket connections |

The webhook-ingress has its own Terraform in `webhook-ingress/infra/` (API Gateway v2, Lambda, SQS, DynamoDB tables, WAF, KEDA ScaledJob + RBAC + network policy).

### Beads (`beads/`)

Issue tracking system that maintains state across agent runs. Tracks which issues are in progress, what stage they're at, and prevents duplicate work. Backed by DynamoDB + S3.

## Deployment

### Automated (recommended)

```bash
# Full platform deploy (includes agent-factory)
./platform/scripts/deploy-all.sh

# Agent-factory only (requires platform already deployed)
./platform/scripts/deploy-all.sh --agent-factory-only
```

### Manual — ARC Runners

```bash
cd modules/agent-factory/infra
terraform init -backend-config=../../../environments/dev/modules/agent-factory-backend.tfvars
terraform apply -var-file=terraform.tfvars -auto-approve
```

### Manual — Webhook Ingress

```bash
cd modules/agent-factory/webhook-ingress/infra
terraform init -backend-config=../../../../environments/dev/modules/webhook-ingress-backend.tfvars -input=false
terraform apply -var="environment=dev" -auto-approve
```

Validate:
```bash
aws apigatewayv2 get-apis --query 'Items[?starts_with(Name,`adp-dev-webhook`)].{Name:Name,Endpoint:ApiEndpoint}'
aws sqs get-queue-url --queue-name adp-dev-agent-submit.fifo
aws dynamodb describe-table --table-name adp-dev-tenant-registry --query 'Table.TableStatus'
```

### GitHub App Setup

A single GitHub App is registered via the Connections UI (Settings → Connections
→ "Set up GitHub App") or the CLI fallback:

```bash
modules/agent-factory/webhook-ingress/scripts/register-github-app.sh <GITHUB_ORG>
```

The script stores credentials in Secrets Manager and validates the App's
permissions/events against the expected set (warnings only).

> **ARC runner path (legacy):** If using per-role Apps for self-hosted runners,
> see `modules/agent-factory/SETUP-GUIDE.md` for manual App creation.

## Triggering Agents

### Via GitHub Labels (Actions path)

```bash
gh issue create --title "Add /hello endpoint" --body "Return {\"message\": \"hello\"}"
gh issue edit <NUMBER> --add-label "agent-developer"
```

### Via Webhook (hosted multi-tenant path)

Register the webhook ingress endpoint as a GitHub App webhook URL. Events flow automatically: issue labeled → webhook → Lambda → SQS → KEDA → agent pod.

### Via WebSocket (real-time)

Connect to the WebSocket API Gateway endpoint. Send a JSON task message. Agent gateway handles routing, queuing, and streaming the response back.

### Auto-Trigger Chain

When an agent creates a PR from an `agent/*` branch, `pr-review-trigger.yml` automatically labels it `agent-reviewer` → review agent fires → reviews → merges (if approved).

## Onboarding Other Repos

```bash
# Copy client workflows to target repo
cp -r client-workflows/.github/workflows/ /path/to/target-repo/.github/workflows/

# Or use the onboarding script
cd scripts && ./full-onboard-repo.sh <repo-name>
```

Client workflows use `workflow_call` to invoke centralized agent code — updates propagate automatically.

## Cost Model

| Component | Monthly Cost | Notes |
|-----------|-------------|-------|
| ARC Controller | ~$5 | Small pod on shared EKS |
| Runner Pods | Variable | Scale to 0 when idle |
| Webhook Ingress (API GW + Lambda) | ~$1-5 | Pay-per-request, minimal at low volume |
| DynamoDB (tenant-registry, events, rate-limits, beads) | ~$0-5 | PAY_PER_REQUEST |
| Secrets Manager | ~$2.40 | 6 secrets × $0.40 |
| Bedrock (Claude) | Per-token | Main cost driver |
| WebSocket API GW | Per-message | Minimal for low-volume |

Idle cost is near-zero. Runner pods and KEDA ScaledJobs scale to zero when no work is queued.

## Testing

```bash
cd modules/agent-factory

# Unit tests (fast, no AWS)
uv run pytest tests/ -v

# Webhook ingress tests
cd webhook-ingress && uv run pytest tests/ -v

# Agent runtime tests
cd agent && npm test

# Live tests (requires deployed environment)
TEST_ENV=dev uv run pytest tests/ -v -m "live or not live_only"
```

## Environment Variables (Agent Runtime)

| Variable | Description |
|----------|-------------|
| `CLAUDE_CODE_USE_BEDROCK` | Set to `1` — routes Claude SDK through Bedrock |
| `ANTHROPIC_MODEL` | Bedrock model ID (e.g., `us.anthropic.claude-sonnet-4-6-v1`) |
| `GITHUB_TOKEN` | GitHub App installation token (fetched at runtime) |
| `AGENT_TYPE` | Persona: `developer`, `architect`, `pm`, `reviewer`, `product`, `operations` |
| `ISSUE_NUMBER` | GitHub issue number being worked on |
| `REPO_OWNER` / `REPO_NAME` | Target repository |
| `WORK_DIR` | Path to cloned target repo |

## Directory Structure

```
modules/agent-factory/
├── agent/                    # Agent runtime (TypeScript)
├── webhook-ingress/          # Multi-tenant webhook ingress (Lambda + API GW + SQS)
│   ├── lambda/               # Lambda handlers (github/ + common/)
│   ├── infra/                # Terraform (API GW v2, SQS, DDB, WAF, KEDA ScaledJob)
│   ├── scripts/              # Package + register helpers
│   └── tests/                # E2E + unit tests
├── gateway/                  # Real-time agent delivery (WebSocket + SQS + KEDA)
│   ├── app/                  # SQS consumer + persona routing
│   ├── lambdas/              # Ingest + response Lambdas
│   └── k8s/                  # KEDA ScaledJob manifests
├── rules/                    # Agent behavior rules (personas, phases, templates)
├── infra/                    # Terraform (ARC, IRSA, secrets, beads, SQS, WS API GW)
├── beads/                    # Issue tracking state config
├── docker/                   # Supporting containers
├── scripts/                  # Build, deploy, onboarding scripts
├── client-workflows/         # Reusable workflow callers for other repos
├── actions/                  # Custom GitHub Actions
├── agent-worker-image/       # Worker container image definition
├── runner-infra/             # Reference: standalone runner infra
└── tests/                    # Integration + live tests
```

## Further Reading

- [SETUP-GUIDE.md](SETUP-GUIDE.md) — Detailed step-by-step deployment guide
- [AGENTS.md](AGENTS.md) — Agent-specific instructions and behavior rules
- [webhook-ingress/README.md](webhook-ingress/README.md) — Webhook ingress details
- [gateway/docs/intelligent-routing.md](gateway/docs/intelligent-routing.md) — Routing logic
- [tests/README.md](tests/README.md) — Test suite documentation
