# OpenClaw Use Cases — Research Catalog

> Benchmarking target: [OpenClaw](https://github.com/openclaw/openclaw) (personal AI assistant, 24+ channels, multi-agent, local-first).
>
> **Scope**: Only use cases relevant to our **agent-gateway** path (ingest Lambda, classifier, SQS/KEDA chat-agent worker, response Lambda, WS/Slack/REST delivery). See issue #95 for exclusions.
>
> **Methodology**: Sources include OpenClaw repo (README, docs/concepts, docs/automation, docs/plugins, extensions), HackerNews threads, blog posts, and GitHub issues/discussions.

---

## Legend

| Signal | Meaning |
|--------|---------|
| :fire: | >= 5 independent sources |
| :pushpin: | 2-4 independent sources |
| :paperclip: | 1 source |

---

## Use Cases

### 1. Multi-turn conversational chat over WebSocket

**Description**: User opens a persistent WebSocket connection and has a multi-turn conversation with an AI agent, with context preserved across turns within the session.

**Category**: `chat-ui`

**Sources**:
- OpenClaw README: "answers you on the channels you already use" — webchat is the primary channel
- OpenClaw `docs/concepts/session.md`: "All session state is owned by the gateway... Transcripts store as JSONL"
- OpenClaw `docs/concepts/context-engine.md`: "Assemble — called before model runs, returning ordered messages within the token budget"
- HackerNews: "OpenClaw is what Apple intelligence should have been" (518 pts) — describes interactive chat
- OpenClaw blog: "Web-based chat interface with image upload functionality"

**Popularity**: :fire:

---

### 2. Slack bot: respond to DMs and @mentions

**Description**: User DMs the bot or @mentions it in a Slack channel; the agent processes the message and replies in-thread.

**Category**: `slack-bot`

**Sources**:
- OpenClaw `extensions/slack/` — 104+ source files for Slack integration
- OpenClaw `docs/concepts/session.md`: "Direct messages: Shared session by default... Group chats: Isolated per group"
- OpenClaw `docs/concepts/typing-indicators.md`: "Direct chats & group mentions: typing starts immediately"
- HackerNews discussion: users describe Slack integration as primary use case
- OpenClaw README: lists Slack among supported channels

**Popularity**: :fire:

---

### 3. Discord bot: channel and DM responses

**Description**: Deploy an AI assistant as a Discord bot responding to DMs and channel mentions.

**Category**: `discord-bot`

**Sources**:
- OpenClaw `extensions/discord/` — dedicated extension
- OpenClaw README: lists Discord among 24+ supported channels
- OpenClaw `docs/concepts/streaming.md`: "Discord uses draft chunking"
- HackerNews users describe Discord bot use case

**Popularity**: :pushpin:

---

### 4. Telegram bot interactions

**Description**: AI assistant responds to Telegram messages with streaming preview updates.

**Category**: `telegram-bot`

**Sources**:
- OpenClaw `extensions/telegram/` — dedicated extension
- OpenClaw README: lists Telegram among channels
- OpenClaw `docs/concepts/streaming.md`: "Telegram uses send/edit patterns"

**Popularity**: :pushpin:

---

### 5. WhatsApp business bot

**Description**: Route WhatsApp messages to an AI agent for customer service or personal assistant use.

**Category**: `whatsapp-bot`

**Sources**:
- OpenClaw `extensions/whatsapp/` — dedicated extension with media handling
- OpenClaw `docs/concepts/multi-agent.md`: "WhatsApp DMs can route to different agents by sender E.164 number"
- OpenClaw README: lists WhatsApp as supported

**Popularity**: :pushpin:

---

### 6. Microsoft Teams bot

**Description**: AI assistant responding in Teams channels and DMs.

**Category**: `teams-bot`

**Sources**:
- OpenClaw `extensions/microsoft-teams/` — dedicated extension
- OpenClaw README: lists Teams as supported

**Popularity**: :paperclip:

---

### 7. Message classification and routing (direct vs long-running)

**Description**: Automatically classify incoming messages to determine if they should be answered directly (simple Q&A) or routed to a long-running agent task.

**Category**: `routing`

**Sources**:
- OpenClaw `docs/concepts/queue.md`: "Five queue modes determine how inbound messages are processed: steer, followup, collect, steer-backlog, interrupt"
- OpenClaw `docs/concepts/agent.md`: "configurable steering modes (steer, followup, collect)"
- OpenClaw `docs/concepts/multi-agent.md`: "Deterministic Routing — messages route to agents based on a specificity hierarchy"
- OpenClaw `src/channels/session.ts` (referenced in our base.py header)

**Popularity**: :pushpin:

---

### 8. Per-session message serialization (FIFO ordering)

**Description**: Ensure messages within a single user session are processed in order, preventing race conditions when a user sends multiple messages quickly.

**Category**: `concurrency`

**Sources**:
- OpenClaw `docs/concepts/queue.md`: "Session lanes: Guarantee only one active run per conversation"
- OpenClaw `docs/concepts/queue.md`: "Debouncing: Default 1-second wait prevents rapid continue patterns"
- OpenClaw `docs/concepts/queue.md`: "serialize inbound auto-reply runs through a tiny in-process queue"

**Popularity**: :pushpin:

---

### 9. Typing/progress indicators during agent processing

**Description**: Show the user that the agent is "thinking" while processing, via typing indicators or progress messages.

**Category**: `chat-ui`

**Sources**:
- OpenClaw `docs/concepts/typing-indicators.md`: configurable modes (never, instant, thinking, message)
- OpenClaw `docs/concepts/streaming.md`: "Tool-progress status lines keep multi-step operations visually responsive"
- OpenClaw `extensions/slack/src/draft-stream.ts`: streaming typing proxy
- HackerNews discussions mention UX responsiveness

**Popularity**: :pushpin:

---

### 10. Context window management with summarization/compaction

**Description**: When conversation history exceeds the model's context window, automatically summarize older messages to fit within budget.

**Category**: `context-management`

**Sources**:
- OpenClaw `docs/concepts/context-engine.md`: "Compact — triggered when context fills, summarizing older history"
- OpenClaw `docs/concepts/context-engine.md`: "Token budget enforcement... compaction summarizes older messages to free space"
- OpenClaw `docs/concepts/session-pruning.md`: session pruning independently trims old tool results
- OpenClaw `docs/concepts/context.md`: context handling concepts

**Popularity**: :pushpin:

---

### 11. Cross-session memory (facts, preferences, learnings)

**Description**: Persist knowledge across sessions — user preferences, learned facts, operational learnings — so the agent recalls them in future conversations.

**Category**: `memory`

**Sources**:
- OpenClaw `docs/concepts/memory.md`: "MEMORY.md — Long-term durable facts, preferences, and decisions loaded at session start"
- OpenClaw `docs/concepts/memory.md`: "memory_search: Employs semantic search to locate relevant notes"
- OpenClaw `docs/concepts/active-memory.md`: active memory patterns
- OpenClaw `extensions/memory-wiki/`: wiki-based memory
- OpenClaw `docs/concepts/dreaming.md`: background memory consolidation

**Popularity**: :fire:

---

### 12. Persona/agent personality customization

**Description**: Configure distinct agent personalities with custom system prompts, behavioral guidelines, and communication styles.

**Category**: `persona`

**Sources**:
- OpenClaw `docs/concepts/agent.md`: "SOUL.md for persona and behavioral boundaries"
- OpenClaw `docs/concepts/agent.md`: "IDENTITY.md and USER.md for identity details"
- OpenClaw `docs/concepts/multi-agent.md`: "Multiple Personalities & Accounts — each agent maintains separate authentication"
- HackerNews: "personal AI assistant" framing emphasizes personalization

**Popularity**: :pushpin:

---

### 13. Multi-agent routing: different agents for different channels/topics

**Description**: Route messages from different channels or about different topics to specialized agents with distinct capabilities.

**Category**: `multi-agent`

**Sources**:
- OpenClaw `docs/concepts/multi-agent.md`: "Multiple isolated agents running within a single Gateway process"
- OpenClaw `docs/concepts/multi-agent.md`: "Most-specific wins ensures predictable message handling"
- OpenClaw `docs/concepts/multi-agent.md`: "One Channel, Multiple Agents — WhatsApp DMs can route to different agents by sender"
- OpenClaw README: "Route inbound channels/accounts/peers to isolated agents across multiple workspaces"

**Popularity**: :pushpin:

---

### 14. Tool use: web search during conversation

**Description**: Agent performs web searches mid-conversation to answer questions that need current information.

**Category**: `tool-use`

**Sources**:
- OpenClaw `extensions/` includes DuckDuckGo, Brave, Exa, Tavily, FireCrawl, SearXNG search extensions
- OpenClaw README: "Browser access" listed as tool
- OpenClaw `docs/plugins/agent-tools.md`: agent tool system
- HackerNews users mention web search as core capability

**Popularity**: :fire:

---

### 15. Tool use: file read/write/edit operations

**Description**: Agent reads, writes, and edits files in a workspace as part of task execution.

**Category**: `tool-use`

**Sources**:
- OpenClaw `docs/concepts/agent.md`: "read/exec/edit/write and related system tools"
- OpenClaw `docs/concepts/agent-workspace.md`: "tools resolve relative paths against the workspace directory"
- OpenClaw AGENTS.md: "Repository structure maps core TypeScript code"

**Popularity**: :pushpin:

---

### 16. Tool use: shell/command execution

**Description**: Agent executes shell commands to accomplish tasks (build, test, deploy, gather system info).

**Category**: `tool-use`

**Sources**:
- OpenClaw `docs/concepts/agent.md`: "read/exec/edit/write and related system tools"
- OpenClaw `docs/concepts/agent-workspace.md`: workspace as tool execution context
- HackerNews: discussions about OpenClaw running commands

**Popularity**: :pushpin:

---

### 17. Artifact publishing: generate and deliver files to user

**Description**: Agent creates files (reports, code, documents) and delivers them to the user via download link or inline.

**Category**: `artifacts`

**Sources**:
- OpenClaw blog: "Web-based chat interface with image upload functionality"
- OpenClaw `docs/concepts/streaming.md`: block streaming for content delivery
- OpenClaw `extensions/` include image generation and media processing

**Popularity**: :pushpin:

---

### 18. Image understanding: process user-uploaded images

**Description**: User sends an image (screenshot, diagram, photo) and the agent analyzes/describes it.

**Category**: `media`

**Sources**:
- OpenClaw blog: "Web-based chat interface with image upload functionality"
- OpenClaw `extensions/media-understanding/` — dedicated extension
- OpenClaw webchat adapter supports attachments

**Popularity**: :pushpin:

---

### 19. Channel-aware response formatting

**Description**: Adapt response format based on delivery channel constraints (Slack mrkdwn, webchat HTML, Discord limits).

**Category**: `channel-formatting`

**Sources**:
- OpenClaw `extensions/slack/src/blocks-render.ts`: Block Kit rendering
- OpenClaw `extensions/slack/src/format.ts`: message formatting
- OpenClaw `docs/concepts/streaming.md`: channel-specific streaming implementations
- OpenClaw `docs/plugins/message-presentation.md`: message presentation plugin

**Popularity**: :pushpin:

---

### 20. Reconnection resilience: deliver replies after WS disconnect/reconnect

**Description**: If a user's WebSocket connection drops mid-task, the agent's reply is still delivered when they reconnect.

**Category**: `reliability`

**Sources**:
- OpenClaw `docs/concepts/session.md`: session persistence survives reconnections
- OpenClaw `docs/concepts/queue.md`: messages are queued, not lost
- HackerNews: reliability discussions in the 1099-pt thread

**Popularity**: :pushpin:

---

### 21. Cron-scheduled autonomous tasks

**Description**: Schedule recurring agent tasks (daily summaries, monitoring checks, report generation) using cron expressions.

**Category**: `automation`

**Sources**:
- OpenClaw `docs/automation/cron-jobs.md`: "One-shot scheduling, Fixed interval, Cron expressions"
- OpenClaw `docs/automation/cron-vs-heartbeat.md`: comparison of scheduling strategies
- OpenClaw `docs/automation/standing-orders.md`: "permanent operating authority for defined programs"
- OpenClaw README: "cron job automation" listed as tool

**Popularity**: :pushpin:

---

### 22. Webhook-triggered agent tasks

**Description**: External systems trigger agent tasks via HTTP webhooks (CI/CD events, monitoring alerts, form submissions).

**Category**: `automation`

**Sources**:
- OpenClaw `docs/automation/webhook.md`: webhook automation (redirects to cron-jobs#webhooks)
- OpenClaw `docs/plugins/webhooks.md`: webhook plugin docs
- OpenClaw `docs/concepts/session.md`: "Webhooks: Isolated per hook"

**Popularity**: :pushpin:

---

### 23. Model failover and resilience

**Description**: Automatically retry with fallback models when the primary model is rate-limited, overloaded, or unavailable.

**Category**: `reliability`

**Sources**:
- OpenClaw `docs/concepts/model-failover.md`: two-stage failure handling (auth profile rotation + model fallback)
- OpenClaw `docs/concepts/retry.md`: retry mechanisms
- HackerNews: 802-pt thread about Google restricting accounts, highlighting need for failover

**Popularity**: :pushpin:

---

### 24. Per-user session isolation (multi-tenant)

**Description**: Ensure different users' conversations are isolated — one user cannot see another's session data.

**Category**: `multi-tenant`

**Sources**:
- OpenClaw `docs/concepts/session.md`: "dmScope: per-channel-peer — isolation by channel and sender (recommended)"
- OpenClaw `docs/concepts/multi-agent.md`: "Credentials are per-agent only — never auto-shared"
- OpenClaw `docs/concepts/session.md`: "multi-user setups — privacy issue where users can access each other's conversations"

**Popularity**: :pushpin:

---

### 25. Thread-aware conversation management

**Description**: Support multiple concurrent conversation threads within a single session, each tracked independently.

**Category**: `threading`

**Sources**:
- OpenClaw `extensions/slack/src/threading.ts`: thread management
- OpenClaw `docs/concepts/session.md`: session routing by thread
- OpenClaw `docs/concepts/queue.md`: session lanes per conversation

**Popularity**: :pushpin:

---

### 26. Message deduplication

**Description**: Prevent duplicate processing when the same message is delivered multiple times (retries, webhook replay).

**Category**: `reliability`

**Sources**:
- OpenClaw `docs/concepts/queue.md`: "Message cap: Maximum 20 queued messages per session"
- OpenClaw `docs/concepts/queue.md`: debouncing prevents rapid duplicate patterns
- HackerNews reliability discussions

**Popularity**: :paperclip:

---

### 27. Follow-up message buffering during active tasks

**Description**: When the agent is busy processing a task, buffer any follow-up messages and process them when the current task completes.

**Category**: `concurrency`

**Sources**:
- OpenClaw `docs/concepts/queue.md`: "followup: Enqueue for next agent turn after current completes"
- OpenClaw `docs/concepts/queue.md`: "collect: Coalesce multiple queued messages into a single response"
- OpenClaw `docs/concepts/queue.md`: "Overflow policy: Dropped messages can be summarized"

**Popularity**: :pushpin:

---

### 28. Escalation to code tasks (agent creates GitHub issues)

**Description**: When a chat conversation identifies a code change need, the agent escalates by creating a GitHub issue and dispatching a specialized agent.

**Category**: `github-dispatch`

**Sources**:
- OpenClaw `docs/concepts/delegate-architecture.md`: tiered delegation model
- OpenClaw `extensions/` include GitHub-related integrations
- HackerNews: discussions about code automation capabilities

**Popularity**: :paperclip:

---

### 29. Voice interaction: speech-to-text and text-to-speech

**Description**: Users interact with the AI via voice — speaking questions and hearing responses.

**Category**: `voice`

**Sources**:
- OpenClaw README: "Wake word detection on macOS/iOS; continuous voice mode on Android"
- OpenClaw `docs/plugins/voice-call.md`: Twilio, Telnyx, Plivo voice providers
- OpenClaw `docs/tts.md`: text-to-speech documentation
- OpenClaw README: "speak and listen on macOS/iOS/Android"

**Popularity**: :pushpin:

---

### 30. Live Canvas: interactive visual workspace

**Description**: Render an interactive visual canvas where the agent can display and manipulate UI elements, charts, or documents.

**Category**: `canvas-ui`

**Sources**:
- OpenClaw README: "Live Canvas workspace with A2UI (agent-driven visual interface)"
- OpenClaw `docs/concepts/agent-workspace.md`: "canvas/ directory supports Canvas UI files for node displays"

**Popularity**: :paperclip:

---

### 31. Background memory consolidation ("Dreaming")

**Description**: Automatically consolidate short-term memory signals into durable long-term memory during idle periods.

**Category**: `memory`

**Sources**:
- OpenClaw `docs/concepts/dreaming.md`: "Three-Phase Architecture: Light Phase, Deep Phase, REM Phase"
- OpenClaw `docs/concepts/dreaming.md`: "opt-in, disabled by default; default cadence runs at 3 AM UTC"
- OpenClaw `docs/concepts/memory.md`: "DREAMS.md — Optional human-reviewable consolidation summaries"

**Popularity**: :paperclip:

---

### 32. Standing orders: autonomous recurring agent programs

**Description**: Define persistent programs with scope, triggers, and approval gates that let agents operate autonomously on schedule.

**Category**: `automation`

**Sources**:
- OpenClaw `docs/automation/standing-orders.md`: "permanent operating authority for defined programs"
- OpenClaw `docs/automation/standing-orders.md`: "Execute-Verify-Report" pattern
- OpenClaw `docs/concepts/delegate-architecture.md`: "Tier 3 (Proactive): operates autonomously on schedules"

**Popularity**: :paperclip:

---

### 33. Gmail/email integration: respond to emails

**Description**: Monitor inbox and respond to emails using the AI agent.

**Category**: `email-bot`

**Sources**:
- OpenClaw `docs/automation/gmail-pubsub.md`: Gmail Pub/Sub integration
- OpenClaw `docs/automation/auth-monitoring.md`: authentication monitoring

**Popularity**: :paperclip:

---

### 34. Image generation during conversation

**Description**: Agent generates images (diagrams, illustrations) during conversation and delivers them to the user.

**Category**: `media`

**Sources**:
- OpenClaw `extensions/image-generation/` — dedicated extension
- OpenClaw `extensions/` includes video generation providers (Runway, FAL)

**Popularity**: :paperclip:

---

### 35. Browser automation during tasks

**Description**: Agent navigates web pages, fills forms, or extracts data using a headless browser.

**Category**: `tool-use`

**Sources**:
- OpenClaw README: "Browser access" as a tool capability
- OpenClaw `extensions/` include browser automation
- OpenClaw `docs/automation/cron-jobs.md`: "Browser cleanup: Isolated runs automatically close associated browser processes"

**Popularity**: :pushpin:

---

### 36. Delegation with tiered authorization

**Description**: Agent operates on behalf of users with graduated capability tiers (read-only draft, send on behalf, proactive).

**Category**: `delegation`

**Sources**:
- OpenClaw `docs/concepts/delegate-architecture.md`: "Tier 1 (Read-Only + Draft), Tier 2 (Send on Behalf), Tier 3 (Proactive)"
- OpenClaw `docs/concepts/delegate-architecture.md`: "Hard blocks in SOUL.md and AGENTS.md"

**Popularity**: :paperclip:

---

### 37. Multi-model provider support

**Description**: Route requests to different LLM providers (Anthropic, OpenAI, Google, local models) based on configuration or task type.

**Category**: `model-routing`

**Sources**:
- OpenClaw `extensions/` include 15+ model providers (OpenAI, Anthropic, Google, Groq, Mistral, etc.)
- OpenClaw `docs/concepts/models.md`: model configuration
- OpenClaw `docs/concepts/model-providers.md`: provider docs
- HackerNews: 1099-pt thread about Anthropic restricting OpenClaw (provider diversity needed)
- HackerNews: 802-pt thread about Google restricting users

**Popularity**: :fire:

---

### 38. Sandboxed tool execution

**Description**: Execute agent tools in an isolated sandbox to prevent unintended system modifications.

**Category**: `security`

**Sources**:
- OpenClaw `docs/concepts/agent-workspace.md`: "sandboxing restricts tool operations to isolated sandbox workspaces"
- HackerNews: 514-pt thread about OpenClaw privilege escalation vulnerability (CVE-2026-33579)
- OpenClaw `docs/security/`: security documentation

**Popularity**: :pushpin:

---

### 39. Skills system: extensible agent capabilities

**Description**: Load modular skill definitions that extend agent capabilities, with priority-based resolution from workspace, personal, and bundled sources.

**Category**: `extensibility`

**Sources**:
- OpenClaw `docs/concepts/agent.md`: "loads extensible skills from a prioritized hierarchy"
- OpenClaw `docs/plugins/skill-workshop.md`: skill workshop
- OpenClaw `docs/concepts/agent.md`: "Skills can be gated through configuration and environment variables"

**Popularity**: :pushpin:

---

### 40. Large payload chunking for delivery

**Description**: Split large agent responses into multiple frames/messages to stay within channel size limits.

**Category**: `reliability`

**Sources**:
- OpenClaw `docs/concepts/streaming.md`: "EmbeddedBlockChunker to control output through minChars/maxChars"
- OpenClaw `docs/concepts/streaming.md`: "Break preference hierarchy: Paragraph > newline > sentence > whitespace > hard break"
- OpenClaw Slack extension: handles Slack's 3000-char limit

**Popularity**: :paperclip:

---

### 41. OAuth authentication for channel access

**Description**: Authenticate users via OAuth flows for channel integrations (Slack, Google, etc.).

**Category**: `auth`

**Sources**:
- OpenClaw `docs/concepts/oauth.md`: OAuth documentation
- OpenClaw `docs/concepts/model-failover.md`: "Auth profiles for both API keys and OAuth tokens"
- OpenClaw `docs/automation/auth-monitoring.md`: auth monitoring

**Popularity**: :paperclip:

---

### 42. Interactive message actions (Slack buttons, reactions)

**Description**: Support interactive UI elements in channel messages (buttons, menus, reactions) that trigger agent actions.

**Category**: `interactive-ui`

**Sources**:
- OpenClaw `extensions/slack/src/interactive-replies.ts`
- OpenClaw `extensions/slack/src/message-actions.ts`
- OpenClaw `extensions/slack/src/actions.ts`
- OpenClaw `extensions/discord/` includes interaction handling

**Popularity**: :paperclip:

---

### 43. Usage tracking and cost monitoring

**Description**: Track token usage, API costs, and model invocations across sessions and agents.

**Category**: `observability`

**Sources**:
- OpenClaw `docs/concepts/usage-tracking.md`: usage tracking
- OpenClaw `extensions/opentelemetry-diagnostics/`: observability extension
- HackerNews: extensive discussion about token costs in the 1099-pt thread

**Popularity**: :pushpin:

---

### 44. Heartbeat/keep-alive during long operations

**Description**: Send periodic heartbeat signals to keep connections alive during long-running agent operations.

**Category**: `reliability`

**Sources**:
- OpenClaw `docs/automation/cron-vs-heartbeat.md`: heartbeat mechanism
- OpenClaw `docs/concepts/streaming.md`: "Tool-progress status lines keep multi-step operations visually responsive"
- OpenClaw `docs/concepts/typing-indicators.md`: typing refresh interval

**Popularity**: :pushpin:

---

### 45. Session reset and lifecycle management

**Description**: Automatically or manually reset sessions (daily reset, idle timeout, user command) to manage context freshness.

**Category**: `session-mgmt`

**Sources**:
- OpenClaw `docs/concepts/session.md`: "Daily reset at 4:00 AM, Idle reset after inactivity, Manual /new or /reset"
- OpenClaw `docs/concepts/session-pruning.md`: session pruning
- OpenClaw `docs/concepts/session.md`: "30 days default retention, 500 maximum entries"

**Popularity**: :paperclip:

---

### 46. Media attachment handling (file uploads)

**Description**: Process file attachments sent by users (documents, images, audio) and use them in the conversation context.

**Category**: `media`

**Sources**:
- OpenClaw `extensions/slack/src/actions.ts`: file download and processing
- OpenClaw `extensions/whatsapp/src/media.ts`: WhatsApp media handling
- OpenClaw `extensions/media-understanding/`: media processing
- OpenClaw blog: "image upload functionality"

**Popularity**: :pushpin:

---

### 47. Multi-step planning and research tasks

**Description**: Agent performs complex multi-step research or planning tasks involving multiple tool calls and reasoning steps before delivering a final answer.

**Category**: `multi-step-planning`

**Sources**:
- OpenClaw `docs/concepts/agent-loop.md`: agent loop with multi-turn tool use
- OpenClaw `docs/concepts/context-engine.md`: context management for long conversations
- OpenClaw `docs/concepts/queue.md`: "steer: Inject immediately into current run"
- HackerNews: users describe research assistant use cases

**Popularity**: :fire:

---

### 48. Observability: OpenTelemetry tracing

**Description**: Instrument agent operations with distributed tracing for debugging and performance monitoring.

**Category**: `observability`

**Sources**:
- OpenClaw `extensions/opentelemetry-diagnostics/`: dedicated extension
- OpenClaw `docs/diagnostics/`: diagnostics documentation
- OpenClaw `docs/logging.md`: logging documentation

**Popularity**: :paperclip:

---

### 49. iMessage integration

**Description**: AI assistant accessible via iMessage (Apple Messages) on macOS/iOS.

**Category**: `imessage-bot`

**Sources**:
- OpenClaw `extensions/imessage/` and `extensions/bluebubbles/` — dedicated extensions
- OpenClaw README: lists iMessage among supported channels

**Popularity**: :paperclip:

---

### 50. Nostr/Matrix/IRC federation support

**Description**: AI agent operates on decentralized/federated messaging protocols.

**Category**: `federation-bot`

**Sources**:
- OpenClaw `extensions/nostr/`, `extensions/matrix/`, `extensions/irc/` — dedicated extensions
- OpenClaw README: lists Nostr, Matrix, IRC among channels

**Popularity**: :paperclip:

---

## Category Summary

| Category | Count | Examples |
|----------|-------|---------|
| chat-ui | 2 | #1, #9 |
| slack-bot | 1 | #2 |
| discord-bot | 1 | #3 |
| telegram-bot | 1 | #4 |
| whatsapp-bot | 1 | #5 |
| teams-bot | 1 | #6 |
| routing | 1 | #7 |
| concurrency | 2 | #8, #27 |
| context-management | 1 | #10 |
| memory | 2 | #11, #31 |
| persona | 1 | #12 |
| multi-agent | 1 | #13 |
| tool-use | 4 | #14, #15, #16, #35 |
| artifacts | 1 | #17 |
| media | 3 | #18, #34, #46 |
| channel-formatting | 1 | #19 |
| reliability | 4 | #20, #26, #40, #44 |
| automation | 3 | #21, #22, #32 |
| multi-tenant | 1 | #24 |
| threading | 1 | #25 |
| github-dispatch | 1 | #28 |
| voice | 1 | #29 |
| canvas-ui | 1 | #30 |
| delegation | 1 | #36 |
| model-routing | 1 | #37 |
| security | 1 | #38 |
| extensibility | 1 | #39 |
| auth | 1 | #41 |
| interactive-ui | 1 | #42 |
| observability | 2 | #43, #48 |
| session-mgmt | 1 | #45 |
| email-bot | 1 | #33 |
| imessage-bot | 1 | #49 |
| federation-bot | 1 | #50 |
