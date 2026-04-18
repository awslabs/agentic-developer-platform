# Agent Factory

Autonomous code agents powered by Claude SDK and Amazon Bedrock, orchestrated via GitHub Actions.

## Overview

Agent Factory provides a team of AI agent personas that work on GitHub issues autonomously. When an issue is labeled with an agent trigger (e.g., `agent-developer`), the corresponding agent clones the repo, analyzes the issue, generates a plan, implements changes, and creates a pull request.

## Agent Personas

| Agent | Label | Role |
|-------|-------|------|
| `@agent-pm` | `agent-pm` | Project management, issue decomposition, sub-issue creation |
| `@agent-architect` | `agent-architect` | Architecture design, technical decisions, units generation |
| `@agent-product` | `agent-product` | Requirements analysis, user stories, acceptance criteria |
| `@agent-developer` | `agent-developer` | Code implementation, unit tests, PR creation |
| `@agent-reviewer` | `agent-reviewer` | Code review, integration testing, PR merge |
| `@agent-operations` | `agent-operations` | Infrastructure, deployment, monitoring, runbooks |

## Architecture

```
GitHub Issue (labeled)
        |
        v
GitHub Actions Workflow (on shared EKS via ARC)
        |
        v
Agent Runtime (TypeScript + Claude SDK)
        |
        ├── Reads issue + repo context
        ├── Generates implementation plan
        ├── Executes via Claude Code (Bedrock)
        ├── Commits changes + creates PR
        └── PR triggers @agent-reviewer automatically
```

## Directory Structure

```
modules/agent-factory/
├── agent/                    # Agent runtime (TypeScript)
│   ├── src/
│   │   ├── components/       # Core: orchestrator, planner, code gen, state
│   │   ├── services/         # Agent service, approval, error recovery
│   │   ├── types/            # TypeScript interfaces
│   │   └── utils/            # Helpers (GitHub API, resilient queries)
│   ├── package.json
│   ├── tsconfig.json
│   └── Dockerfile
├── rules/                    # Agent behavior rules
│   ├── personas/             # Per-agent instructions (developer, architect, etc.)
│   ├── phases/               # SDLC phase guides (inception, construction, ops)
│   ├── templates/            # Output templates
│   └── workflows/            # Workflow templates
├── infra/                    # Terraform (references shared platform)
│   ├── main.tf               # Remote state + agent-specific resources
│   └── modules/
│       ├── runner-iam/        # IRSA role (Bedrock, Secrets Manager, etc.)
│       ├── arc-runner/        # ARC controller + runner scale set (Helm)
│       ├── secrets/           # GitHub App credentials in Secrets Manager
│       └── beads-state/       # DynamoDB + S3 for issue tracking
├── docker/                   # Supporting containers
├── scripts/                  # Build, deploy, onboarding scripts
├── client-workflows/         # Reusable workflow callers for other repos
├── runner-infra/             # Reference: standalone runner infra (for docs)
└── beads/                    # Beads issue tracking config
```

## Infrastructure

Agent Factory shares infrastructure with the gateway module via the platform layer:

| Resource | Source | Notes |
|----------|--------|-------|
| VPC | `platform/infra` (shared) | Same VPC as gateway |
| EKS Cluster | `platform/infra` (shared) | Agents run as pods on shared cluster |
| ECR | `platform/infra` (shared) | `adp-agent-runtime` repo already exists |
| Runner IAM | `modules/agent-factory/infra` | IRSA role with Bedrock + Secrets Manager |
| ARC Controller | `modules/agent-factory/infra` | Helm release on shared EKS |
| Secrets Manager | `modules/agent-factory/infra` | GitHub App credentials per persona |
| Beads State | `modules/agent-factory/infra` | DynamoDB + S3 for issue tracking |

## Setup

### Prerequisites

- Shared platform deployed (`platform/infra/`)
- GitHub Apps created (one per agent persona for rate limit isolation)
- AWS CLI, Terraform, kubectl, Helm

### 1. Deploy Infrastructure

```bash
cd modules/agent-factory/infra

terraform init -backend-config=../../../environments/dev/modules/agent-factory-backend.tfvars
terraform plan -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars
```

### 2. Populate GitHub App Secrets

```bash
# For each persona (dev, pm, ops):
aws secretsmanager put-secret-value \
  --secret-id adp/gh-app-dev-id \
  --secret-string "<APP_ID>"

aws secretsmanager put-secret-value \
  --secret-id adp/gh-app-dev-key \
  --secret-string "$(cat /path/to/private-key.pem)"
```

### 3. Install Workflows in Target Repos

Copy the client workflows to any repo that should use the agents:

```bash
cp -r client-workflows/.github/workflows/ <target-repo>/.github/workflows/
```

### 4. Trigger an Agent

Add a label to any issue:
- `agent-developer` → triggers code implementation
- `agent-pm` → triggers project management
- `agent-architect` → triggers architecture design

## Workflows

The agent workflows live in `.github/workflows/` (required by GitHub Actions) but reference the agent code in `modules/agent-factory/agent/`.

| Workflow | Trigger | Agent |
|----------|---------|-------|
| `agent-developer.yml` | `agent-developer` label | Code implementation |
| `agent-architect.yml` | `agent-architect` label | Architecture design |
| `agent-pm.yml` | `agent-pm` label | Project management |
| `agent-reviewer.yml` | `agent-reviewer` label | Code review + merge |
| `agent-product.yml` | `agent-product` label | Requirements analysis |
| `agent-operations.yml` | `agent-operations` label | Infrastructure + deploy |
| `pr-review-trigger.yml` | PR opened from `agent/*` branch | Auto-triggers reviewer |
| `skill-agent.yml` | `skill-agent` label | Skill-driven agent |

## Testing

See [`tests/README.md`](tests/README.md) for the full test suite documentation.

```bash
# Unit tests (fast, no AWS required)
cd modules/agent-factory
uv run pytest tests/ -v

# Live tests (requires deployed environment)
TEST_ENV=dev uv run pytest tests/ -v -m "live or not live_only"
```

## Development

```bash
cd modules/agent-factory/agent

npm install
npm run build
npm test
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CLAUDE_CODE_USE_BEDROCK` | Yes | Set to `1` |
| `ANTHROPIC_MODEL` | Yes | Bedrock model ID |
| `GITHUB_TOKEN` | Yes | GitHub App token |
| `AGENT_TYPE` | Yes | Persona: developer, architect, pm, reviewer, product, operations |
| `ISSUE_NUMBER` | Yes | GitHub issue number |
| `REPO_OWNER` | Yes | Repository owner |
| `REPO_NAME` | Yes | Repository name |
| `WORK_DIR` | Yes | Path to cloned target repo |
