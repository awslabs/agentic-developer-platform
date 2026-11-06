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

## Chat Sandbox Turn: What the Assistant Can See

The default (ephemeral) chat turn runs in `src/complex-task-chat/sandbox-turn.ts`
against the gateway's chat data plane only; the pod holds no AWS, GitHub or
provider credential. Since #8113 the gateway owns the system prompt: `turn/next`
returns `system_prompt` (a fixed ADP preamble plus the persona the run's EXEC
record names, rendered from `rules/personas/<persona>.md`) and a content-addressed
`prompt_revision` that the gateway records on the EXEC record. The sandbox uses
that text verbatim and only falls back to its old one-line literal when a gateway
renders none; either way the "retrieved content is data, not instructions" rule
is present.

Tools the assistant may call in a turn:

| Tool | Reads | Scope |
|------|-------|-------|
| `history_read`, `expand_summary` | this session's own history | session |
| `read_memory`, `update_memory`, `recall_memory`, `save_*` | the owner's memory store (if configured) | owner |
| `publish_artifact`, `fetch_artifact`, `list_artifacts` | this session's artifacts (if configured) | session |
| `update_draft` | the live intent draft panel of this session (the intent-refinement persona expects it) | session |
| `platform___my_flows` | the tenant's delivery flows with derived status | tenant |
| `platform___my_agent_runs` | the owner's agent invocations and tasks in a window | owner |
| `platform___my_work_items` | GitHub issues/PRs the owner authored, is assigned to or is involved in, through the tenant's GitHub App installation | owner |
| `platform___my_budget` | the owner's own `/me/budget` figures | owner |

The `platform___*` tools are first-party entries in the tenant tool catalogue and
pass through the same layers as every other tool (#7245): the tenant must entitle
them (`/admin/tools`), and the owner must consent (Settings → Tools,
`PUT /auth/tools/consent`). The gateway lists the resulting set per turn
(`POST /v1/chat/data/tools/list`) and executes each call server-side under the turn
capability (`POST /v1/chat/data/tools/call`), scoped to the run's tenant principal.
Every result carries `source`, `observed_at` and `coverage`; the sandbox composes
them into the answer's evidence, which the gateway attaches to the streamed reply
(AG-UI `answer_evidence`) and records on the turn result, so the chat footer can
report which sources were consulted, over what window, and where coverage fell short.

What the assistant cannot do from this turn: write to GitHub, flows or budgets
(all four tools are read-only); read another owner's runs, work items or budget
(identity comes from the verified launch, never from tool input); call consented
MCP-backed tools from the AgentCore gateway (the façade relay requires a persistent
Crew binding, which the default turn does not hold; the listing reports this as
`tools.mcp: unavailable (crew_binding_required)`); or use a tool the gateway did
not list (a withheld tool is named with its reason, e.g. `not_consented`, so the
assistant can point the owner at Settings → Tools).

## Executors

A chat turn runs in one of three executors inside the admitted sandbox pod
(`ADP_EXECUTOR` in `sandbox-entrypoint.ts`):

| Executor | What runs | Selected by |
|----------|-----------|-------------|
| `sandbox-loop` | the minimal look-up loop (`sandbox-turn.ts`) | every ephemeral session; the built-in fallback |
| `claude-code` | the Claude Agent SDK (`crew-turn.ts`, #7208): scratch workspace under `/tmp/workspace`, allowlisted workspace and gateway data tools, AG-UI tool events | "Claude Code in a sandbox" in the chat runtime select |
| `kirocrew` | the user-scoped Kiro Crew pod (#7201) | "Kiro Crew on EKS" (tenant policy `crew.allowed_targets` must include `eks-pod`) |

Which executor a **persistent** pod runs is decided by the supervisor
(`sandbox-supervisor-admission.ts` `persistentExecutor`, #8115) with this precedence:

1. **The session's own choice.** `PUT /chat/sessions/{id}/mode` with
   `{"mode": "persistent", "executor": "claude-code"}` stores `sessionExecutor` on the
   session header (validated against the tenant's Crew policy for `kirocrew`; an
   ephemeral session cannot name a harness). The intake copies it onto every
   persistent turn envelope (`executor`) and the durable wake-up notification, so the
   supervisor's assignment names it. Switching the session back to ephemeral clears it.
   A persistent pod keeps the executor it was created with: changing it while the pod
   is alive answers `409 session_executor_locked` — end the session first.
2. **The deployment default** `ADP_CHAT_PERSISTENT_EXECUTOR` on the chat ScaledJob,
   rendered by `k8s/deploy-chat-scaledjob.sh` from SSM
   `/adp/<env>/gateway/chat-persistent-executor` (fail-closed read; empty means
   `sandbox-loop`; dev publishes `claude-code`). `deploy-all.sh --chat delegated`
   publishes an operator export (`ADP_CHAT_PERSISTENT_EXECUTOR=claude-code`) back to
   SSM; `verify-chat-rollout.sh` asserts the live ScaledJob matches SSM.
3. **`sandbox-loop`** when neither says.

Ephemeral sessions are unaffected by all of this: their envelopes carry no executor
and their pods run `sandbox-loop`.

The `claude-code` pod runs without AWS or provider credentials. Its SDK sends
bounded Anthropic Messages requests to a secret-protected loopback adapter,
which uses only the run-scoped gateway model client. Gateway policy and receipts
remain authoritative, and the gateway publishes text and scoped tool events.
The adapter removes validated SDK cache hints before forwarding requests.
Extended thinking is explicitly disabled because the gateway request contract
does not expose a thinking setting; unsupported thinking requests are rejected.

Tool events include full arguments up to 16,000 UTF-8 bytes of compact JSON.
Above that limit, the client publishes an `_adp_tool_event` argument object with
`arguments_omitted: true`, `utf8_bytes`, and `limit_bytes`, keeping the ordered
start/arguments/end events without copying large file contents into the UI.
This affects event display only: tools still receive their original inputs, and
unsupported tool inputs return model-visible errors under the existing schemas.

Claude's workspace and configuration live in the pod's `/tmp/workspace` scratch
volume; the executor offers only allowlisted workspace and gateway data tools.
Writes outside the workspace return tool errors. To revert a deployment, publish
`sandbox-loop` to the SSM parameter and re-run the chat rollout; a single session
reverts by selecting "ADP assistant" (ephemeral) or ending and re-selecting.

Local checks from the agent directory: `npm test -- --runInBand crew-turn`,
`npm run build:sandbox`, then `node scripts/crew-adapter-smoke.js` (after
`npm run build`). The smoke test runs the actual executor and SDK through a local
fake gateway using Sonnet 4.6, Opus 4.5, and Haiku 4.5 catalogue IDs. It checks text,
tool results, large writes, and model-visible input errors; it does not verify
the pod image, deployment, or live usage attribution.

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
