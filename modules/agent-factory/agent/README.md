# GitHub Agent

Autonomous GitHub agent using Anthropic Agent SDK V2 with Amazon Bedrock.

## Overview

This agent:
1. Triggers when a GitHub issue receives a specific label
2. Analyzes the issue and generates an implementation plan
3. Posts the plan for approval
4. Upon `/approve`, implements the changes
5. Creates a pull request

## Prerequisites

- Node.js 20+
- Infrastructure deployed (see `../infrastructure/`)
- GitHub App configured (see `../setup/`)

## Installation

```bash
npm install
```

## Build

```bash
npm run build
```

## Usage

The agent runs via GitHub Actions workflow. It's triggered automatically when an issue is labeled.

### Manual Testing

```bash
# Set required environment variables
export GITHUB_EVENT_PATH=/path/to/event.json
export AWS_REGION=us-east-1
export CLAUDE_CODE_USE_BEDROCK=1
export ANTHROPIC_MODEL=global.anthropic.claude-opus-4-5-20251101-v1:0

npm start
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GITHUB_EVENT_PATH` | Yes | - | Path to GitHub event JSON |
| `AWS_REGION` | Yes | - | AWS region |
| `CLAUDE_CODE_USE_BEDROCK` | Yes | - | Set to `1` |
| `ANTHROPIC_MODEL` | Yes | - | Bedrock model ID |
| `SECRET_PREFIX` | No | `github-agent` | Secrets Manager prefix |
| `POLLING_INTERVAL` | No | `30000` | Approval polling (ms) |
| `LOG_LEVEL` | No | `INFO` | DEBUG, INFO, WARN, ERROR |
| `MAX_RETRIES` | No | `5` | Max retry attempts |

## Architecture

```
src/
├── index.ts              # Entry point
├── types/                # TypeScript interfaces
├── components/           # Core components
│   ├── ConfigLoader      # Configuration & secrets
│   ├── Logger            # CloudWatch logging
│   ├── WorkspaceManager  # Temp directories
│   ├── TokenManager      # GitHub App tokens
│   ├── GitHubClient      # GitHub API
│   ├── StateManager      # State persistence
│   ├── ProgressTracker   # Checklist updates
│   ├── InputHandler      # Parse GitHub event
│   ├── ConcurrencyGuard  # Lock management
│   ├── PlanningAgent     # Plan generation
│   ├── CodeGenerationAgent # Code generation
│   └── AgentOrchestrator # Workflow coordinator
└── services/
    ├── AgentService      # Top-level orchestration
    ├── ApprovalService   # Approval polling
    └── ErrorRecoveryService # Error handling
```

## Workflow

1. **Trigger**: Issue labeled with `ai-agent`
2. **Initialize**: Load config, acquire lock, setup logging
3. **Planning**: Clone repo, analyze issue, generate plan
4. **Approval**: Post plan, poll for `/approve` or `/reject`
5. **Code Generation**: Execute plan using Claude SDK
6. **PR Creation**: Commit changes, create pull request
7. **Cleanup**: Release lock, cleanup workspace

## Commands

In issue comments:
- `/approve` - Proceed with implementation
- `/reject` - Wait for updated instructions

## Troubleshooting

### Agent not triggering
- Check GitHub Actions workflow is enabled
- Verify label matches `TRIGGER_LABEL`
- Check self-hosted runner is online

### Authentication errors
- Verify GitHub App credentials in Secrets Manager
- Check IAM role has Secrets Manager access

### Bedrock errors
- Verify model access is enabled in Bedrock console
- Check IAM role has Bedrock permissions
