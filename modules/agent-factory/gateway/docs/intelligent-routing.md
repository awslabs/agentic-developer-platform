# Agent Gateway — Intelligent Routing Design

## Overview

The Ingest Lambda acts as a universal front door for all agent interactions. Instead of hardcoded routing rules, it uses a fast Bedrock classifier (Claude Haiku) to determine the optimal execution path for each user message.

## Three Execution Paths

```
User message (Slack / WebSocket / CLI / GitHub)
        │
        ▼
  Ingest Lambda
        │
        ▼
  Channel Adapter → UnifiedMessage → Session Management
        │
        ▼
  Bedrock Classifier (Haiku, ~1s, ~$0.0001/call)
        │
        ├─── direct_response ──→ Respond immediately from Lambda (~1-2s)
        │                        No agent pod, no SQS, no GitHub Actions
        │
        ├─── long_running ─────────→ SQS → KEDA ScaledJob → Agent Pod (~10-30s)
        │                        Multi-turn chat, analysis, planning
        │
        └─── github_actions ───→ workflow_dispatch → ARC Runner (~60-300s)
                                 Code changes, PRs, issue work
                                 Full GitHub Actions console visibility
```

## Path Selection Criteria

### direct_response (~1-2 seconds)
- Greetings, thanks, acknowledgments
- General knowledge questions ("what is a KEDA ScaledJob?")
- Status checks that can be answered from session/DynamoDB context
- Capability questions ("what can you help me with?")
- Clarifying questions back to the user
- Simple follow-ups in an existing conversation

### long_running (~10-30 seconds)
- Multi-turn reasoning that needs conversation history
- Analysis tasks without code changes
- Planning and architecture discussions
- PM/product persona tasks (requirements, user stories)
- Tasks that need tool use but not git operations

### github_actions (~60-300 seconds)
- Code implementation (developer persona + repo reference)
- Code review (reviewer persona + PR reference)
- Infrastructure changes (operations persona + repo reference)
- Architecture design that produces artifacts (architect persona + repo)
- Any task that needs: git clone, branch creation, commits, PR creation

## Classifier Implementation

Single Bedrock invoke in the Ingest Lambda using Claude Haiku for speed and cost:

```python
{
    "model": "us.anthropic.claude-3-5-haiku-20241022-v1:0",
    "max_tokens": 512,
    "messages": [{"role": "user", "content": classification_prompt}],
    "system": CLASSIFIER_SYSTEM_PROMPT
}
```

The classifier returns structured JSON:
```json
{
    "path": "direct_response | long_running | github_actions",
    "persona": "developer | architect | reviewer | operations | pm | product",
    "repo": "owner/repo" or null,
    "issue_number": 42 or null,
    "response": "Direct answer text" or null,
    "reasoning": "Brief explanation of routing decision"
}
```

The `response` field is only populated when `path` is `direct_response`. For other paths, the classifier only decides the route — the actual work happens downstream.

## GitHub Actions Dispatch

When the classifier selects `github_actions`:

1. Ingest Lambda reads GitHub App credentials from Secrets Manager
2. Generates a short-lived installation token (JWT → installation access token)
3. Calls `POST /repos/{owner}/{repo}/actions/workflows/agent-dispatch.yml/dispatches`
4. Passes session_id, agent_type, message, channel, connection_id as workflow inputs
5. The workflow runs on ARC with full Actions console visibility
6. On completion, the workflow calls the Response SQS queue to route the result back

```yaml
# agent-dispatch.yml (in target repos)
on:
  workflow_dispatch:
    inputs:
      session_id: { required: true, type: string }
      agent_type: { required: true, type: string }
      message: { required: true, type: string }
      channel: { required: true, type: string }
      connection_id: { required: false, type: string }
      callback_queue_url: { required: true, type: string }
```

## Session Continuity Across Channels

Sessions are keyed by `{channel}:{channel_id}:{user_id}:{thread_id}`. But when a task references a GitHub issue, the session can be linked:

- Slack: `@adp-agent fix issue #42 on aws-e/adp` → session key includes `github:aws-e/adp:42`
- GitHub: issue #42 labeled `agent-developer` → same session key
- Both channels see the same conversation history in DynamoDB

## Agent Identity

One agent identity across all channels:

| Channel | Identity | How |
|---------|----------|-----|
| Slack | `@adp-agent` | Slack App (Bot Token) |
| GitHub | `@agent-{persona}` | GitHub App |
| WebSocket | "ADP Agent" | Chat UI |
| Teams | `@ADP Agent` | Teams Bot (future) |

The persona (developer, architect, reviewer, etc.) is selected by the classifier based on the task, not by which bot the user talks to.

## Cost Analysis

| Path | Bedrock Cost | Compute Cost | Latency |
|------|-------------|-------------|---------|
| direct_response | ~$0.0002 (Haiku classify + respond) | Lambda only | 1-2s |
| long_running | ~$0.0001 (Haiku classify) + agent cost | KEDA pod | 10-30s |
| github_actions | ~$0.0001 (Haiku classify) + agent cost | ARC runner | 60-300s |

Most Slack interactions will be `direct_response` — the agent feels instant for simple questions.

## Sequence Diagrams

### Direct Response (simple question on Slack)
```
User → Slack → Ingest Lambda → Bedrock Haiku (classify + answer)
                                      │
                                      ▼
                              Response Lambda → Slack (post reply)
```

### SQS/KEDA (multi-turn chat on WebSocket)
```
User → WebSocket → Ingest Lambda → Bedrock Haiku (classify)
                                      │
                                      ▼ path=long_running
                                   SQS Input Queue
                                      │
                                      ▼
                                   KEDA Pod (agent)
                                      │
                                      ▼
                                   SQS Response Queue
                                      │
                                      ▼
                              Response Lambda → WebSocket (push)
```

### GitHub Actions (code task from Slack)
```
User → Slack → Ingest Lambda → Bedrock Haiku (classify)
                                      │
                                      ▼ path=github_actions
                              GitHub API (workflow_dispatch)
                                      │
                                      ▼
                              GitHub Actions (ARC runner)
                              - Full console visibility
                              - Agent runs, creates PR
                                      │
                                      ▼
                              SQS Response Queue (callback)
                                      │
                                      ▼
                              Response Lambda → Slack (post result)
```


## Runtime Strategy: Agent SDK Placement

### The Question
Can the Ingest Lambda use the Claude Agent SDK Python for `direct_response` tasks? This would give tool-use capabilities (session lookup, issue status) for smart conversational responses.

### Container Image Lambda: Cold Start Problem
Switching the Lambda to a container image (needed for Agent SDK + dependencies > 250MB zip limit) introduces cold start latency:

| Deployment Type | Cold Start | Warm Invocation |
|----------------|-----------|-----------------|
| Zip package (current) | 200-500ms | 5-50ms |
| Container image (small, <500MB) | 3-8s | 5-50ms |
| Container image (large, >1GB) | 8-15s | 5-50ms |

A 3-8 second cold start on the Ingest Lambda is unacceptable — this is the front door for every message. A user sends "hello" on Slack and waits 8 seconds before anything happens.

### Recommended Approach: Split by Runtime

Keep the Ingest Lambda as a lightweight zip deployment for fast cold starts. Move Agent SDK usage to the KEDA pod where cold start doesn't matter.

```
Ingest Lambda (zip, fast cold start ~200-500ms)
  ├── Classifier: raw boto3 bedrock-runtime invoke (Haiku)
  ├── direct_response: classifier generates the answer inline (no Agent SDK)
  ├── long_running: enqueue to SQS
  └── github_actions: workflow_dispatch

KEDA Pod (container, cold start irrelevant)
  └── Claude Agent SDK Python with rich tool set
      - Session history, DynamoDB lookups
      - Web search, file analysis
      - Bedrock invoke with conversation context

ARC Runner (container, cold start irrelevant)
  └── Claude Agent SDK TypeScript (existing agent code)
      - Full GitHub integration, git, PRs
```

### Why This Works
- Ingest Lambda stays at 200-500ms cold start (zip with boto3 only)
- `direct_response` uses the same Haiku call that classifies — one invoke does both classification and response generation. No Agent SDK needed.
- `long_running` tasks go to KEDA pods where the Agent SDK Python runs with no cold start penalty (pod startup time is acceptable for 10-30s tasks)
- `github_actions` tasks go to ARC where the TypeScript Agent SDK runs as today

### KEDA Pod with Agent SDK Python
The SQS consumer in the KEDA pod should use `claude-agent-sdk` Python directly instead of subprocess to TypeScript:

```python
from claude_agent_sdk import AgentSession

session = AgentSession(
    model="global.anthropic.claude-sonnet-4-6",
    system=persona.system_prompt,
    tools=[session_lookup_tool, dynamodb_tool, web_search_tool],
)

for event in session.run(messages=conversation_history):
    if event.type == "assistant":
        # Stream progress to Response SQS
        send_progress(task_id, event.content)
    elif event.type == "result":
        send_response(task_id, event.content)
```

This eliminates the Node.js dependency from the KEDA pod entirely. The Dockerfile becomes pure Python — lighter image, faster startup, simpler debugging.

### Future Option: Lambda SnapStart or Provisioned Concurrency
If we later want Agent SDK in Lambda:
- **Lambda SnapStart** (Python): reduces cold start to ~200ms even for container images. Currently in preview for Python.
- **Provisioned Concurrency**: keeps N Lambda instances warm (~$0.015/hr per instance). Good for predictable traffic.
- These are Phase 3 optimizations if the KEDA path latency becomes a problem.

### Summary: SDK Placement by Path

| Path | Runtime | SDK | Cold Start | Why |
|------|---------|-----|-----------|-----|
| direct_response | Lambda (zip) | Raw Bedrock boto3 | 200-500ms | Must be instant |
| long_running | KEDA pod (container) | Agent SDK Python | N/A (pod) | Rich tools, no time pressure |
| github_actions | ARC runner (container) | Agent SDK TypeScript | N/A (runner) | Full GitHub integration |


## Sessions vs Conversation Threads

Sessions and conversation threads are distinct concepts:

### Session
A session represents a user's connection to the gateway from a specific channel. It's keyed by `{channel}:{channel_id}:{user_id}`. A session:
- Tracks the WebSocket connection_id (for response routing)
- Has a 24-hour TTL
- Belongs to one user on one channel
- Can contain multiple conversation threads

### Conversation Thread
A thread is a topical conversation within a session. A user might have:
- Thread A: "fix the login bug on adp" (github_actions, running)
- Thread B: "deploy the monitoring dashboard" (github_actions, running in parallel)
- Thread C: ongoing chat about architecture decisions (long_running)

Each thread has its own message history and its own processing lock.

### DynamoDB Model

```
Session (PK: session_id)
├── connection_id, channel, user_id, expires_at
├── threads: {
│     "thread_abc": {
│         "messages": [...],
│         "processing_task_id": "task-123",
│         "github_issue_url": "https://...",
│         "topic": "login bug fix"
│     },
│     "thread_def": {
│         "messages": [...],
│         "processing_task_id": "task-456",
│         "github_issue_url": "https://...",
│         "topic": "monitoring dashboard"
│     }
│ }
└── active_thread_id: "thread_abc"  (most recent)
```

### Concurrency Rules by Path

| Path | Concurrency | Serialization | Why |
|------|------------|---------------|-----|
| direct_response | Always immediate | None | Independent of any thread, no state mutation |
| github_actions | Always dispatch | None (per thread) | Each task creates its own GitHub issue, runs on its own ARC runner. Fully independent. |
| long_running (same thread) | Serialized | Per-thread lock | Same conversation needs coherent history. Buffer and re-enqueue. |
| long_running (new topic) | Parallel | New thread created | Classifier detects unrelated topic → new thread, no lock conflict |

### How the Classifier Decides Thread Assignment

The classifier already sees the conversation history. It can determine:

1. **Follow-up to in-flight task**: "actually, also add unit tests for that" → same thread as the running task
2. **New independent task**: "deploy the monitoring dashboard" → new thread
3. **Simple question**: "what time is standup?" → direct_response, no thread needed

The classifier returns a `thread_action` field:

```json
{
    "path": "github_actions",
    "thread_action": "new",        // "new" | "follow_up" | "none"
    "follow_up_thread_id": null,   // set when thread_action = "follow_up"
    ...
}
```

### Follow-up Behavior by Path

When a message is a follow-up to an in-flight task:

| In-flight path | Follow-up action |
|---------------|-----------------|
| github_actions | Post as comment on the linked GitHub issue. The running ARC agent may pick it up. |
| long_running | Buffer in thread history. Response Lambda re-enqueues when current task completes. |

### Example: User sends 3 messages in quick succession

```
User: "fix the login bug on adp"
  → Classifier: github_actions, new thread
  → Create issue #47, label agent-developer
  → Notify user: "🔧 Tracking: github.com/aws-e/adp/issues/47"

User: "also deploy the monitoring dashboard on infra-repo"
  → Classifier: github_actions, new thread (different repo)
  → Create issue #12 on infra-repo, label agent-operations
  → Notify user: "🔧 Tracking: github.com/aws-e/infra-repo/issues/12"
  → Both issues running in parallel on separate ARC runners

User: "what's the status of the auth migration?"
  → Classifier: direct_response
  → Answer immediately from Lambda: "The auth migration PR #38 was merged yesterday..."
  → No thread, no lock, instant response
```

All three handled concurrently. No blocking.

### Implementation Status

Current code uses a single `processing_task_id` per session (session-level lock). The thread-based model described above is the target architecture. Migration path:

1. **Phase 1 (current PR)**: Session-level lock, but `direct_response` and `github_actions` bypass it
2. **Phase 2**: Introduce thread model in DynamoDB, per-thread locks, classifier returns `thread_action`
3. **Phase 3**: Thread-aware Response Lambda re-enqueue logic
