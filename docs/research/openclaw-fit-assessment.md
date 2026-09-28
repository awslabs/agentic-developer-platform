# OpenClaw Fit Assessment — Agent Gateway

> For each use case from `docs/openclaw-use-cases.md`, this document assesses whether our **agent-gateway** path can run it today.
>
> **Scope**: `modules/agent-factory/gateway/` (ingest + response Lambdas, channel adapters, classifier, WebSocket API) and `modules/agent-factory/agent/src/complex-task-chat/` (chat-agent worker, LCM context, memory, artifacts, channel profiles, heartbeats). Runtime: KEDA ScaledJob `chat-agent-worker`.
>
> **Statuses**: Supported = code path exists and works. Partial = fundamentally possible but incomplete. Missing = requires new module/service.

---

| # | Use Case | Status | Evidence | Gap (if partial/missing) | Cost-to-add |
|---|----------|--------|----------|--------------------------|-------------|
| 1 | Multi-turn conversational chat over WebSocket | Supported | `gateway/lambdas/ingest/channels/webchat.py:87-166` parses WS events; `gateway/lambdas/ingest/handler.py:271-315` enqueues to SQS FIFO; `agent/src/complex-task-chat/complex-task-chat-agent.ts:112-116` assembles LCM context with prior turns; `agent/src/complex-task-chat/context/lcm/lcm-context.ts:31-75` resolves context within token budget; `gateway/lambdas/response/routers/websocket.py:108-165` delivers reply to WS connection | | |
| 2 | Slack bot: respond to DMs and @mentions | Partial | `gateway/lambdas/ingest/channels/slack.py:146-305` parses Slack Events API (message + app_mention); `gateway/lambdas/ingest/handler.py:40-43` detects Slack channel; `gateway/lambdas/response/routers/slack.py` routes responses. Signature verification implemented at `slack.py:99-144` | Slack response router (`routers/slack.py`) sends replies but lacks Block Kit rendering, interactive replies, and streaming preview updates. No Slack OAuth bot token management in the response path. | M |
| 3 | Discord bot | Missing | No Discord channel adapter exists in `gateway/lambdas/ingest/channels/`. `channels/base.py:45` defines `ChannelType.DISCORD` enum but no adapter implements it | Requires new Discord adapter, Discord bot token management, Discord API response delivery | M |
| 4 | Telegram bot | Missing | No Telegram channel adapter exists in `gateway/lambdas/ingest/channels/` | Requires new Telegram adapter, Bot API integration, webhook handler | M |
| 5 | WhatsApp business bot | Missing | `channels/base.py:41` defines `ChannelType.WHATSAPP` enum but no adapter. No WhatsApp Business API integration | Requires new WhatsApp adapter, Business API integration, media handling | L |
| 6 | Microsoft Teams bot | Missing | `channels/base.py:43` defines `ChannelType.TEAMS` enum but no adapter | Requires Bot Framework adapter, Azure AD integration | L |
| 7 | Message classification and routing | Supported | `gateway/lambdas/ingest/classifier.py:37-71` uses Claude Sonnet for 3-way classification (direct_response, long_running, github_actions); `classifier.py:91-167` returns path, persona, thread assignment; `handler.py:200-207` routes based on classification path | | |
| 8 | Per-session message serialization (FIFO) | Supported | `gateway/lambdas/ingest/handler.py:295-310` uses SQS FIFO with `MessageGroupId=session_id` for per-session serialization; `agent/src/complex-task-chat/sqs-client.ts:103-112` receives one message per KEDA pod; FIFO MessageDeduplicationId at `handler.py:309` prevents replay | | |
| 9 | Typing/progress indicators | Supported | `agent/src/complex-task-chat/run-query.ts:150-169` emits progress events (tool_use, thinking, heartbeat); `agent/src/complex-task-chat/complex-task-chat-agent.ts:150-169` forwards via `sqs.sendProgress()`; `gateway/lambdas/response/handler.py:93-99` tags progress frames; `gateway/lambdas/response/routers/websocket.py:123-137` delivers with `type=progress` | | |
| 10 | Context window management with summarization | Supported | `agent/src/complex-task-chat/context/lcm/lcm-context.ts:31-75` assembles context within token budget; `agent/src/complex-task-chat/context/lcm/compactor.ts` triggers summarization when items exceed threshold; `agent/src/complex-task-chat/context/summarize/bedrock-summarizer.ts` uses Bedrock for summary generation; `agent/src/complex-task-chat/context/eviction/chronological.ts` evicts oldest items first | | |
| 11 | Cross-session memory (facts, preferences, learnings) | Supported | `agent/src/complex-task-chat/memory/types.ts:9-52` defines MemoryProvider with retrieve/save; `agent/src/complex-task-chat/memory/tools.ts:20-104` exposes 4 tools (recall_memory, save_fact, save_preference, save_learning); `agent/src/complex-task-chat/memory/dynamo-memory.ts` persists to DynamoDB; `agent/src/complex-task-chat/complex-task-chat-agent.ts:95-99` retrieves relevant memories per turn | | |
| 12 | Persona/agent personality customization | Supported | `agent/src/complex-task-chat/persona-loader.ts:54-89` loads persona from disk at `/app/personas/<type>.md`; `persona-loader.ts:104-153` composes system prompt from base + learnings + memories; `gateway/app/personas/loader.py` loads personas for the SQS consumer; `classifier.py:43` selects persona (developer, architect, reviewer, operations, pm, product) | | |
| 13 | Multi-agent routing | Supported | `gateway/lambdas/ingest/classifier.py:43` classifies to 6 persona types; `handler.py:300` includes `agent_type` in SQS payload; `complex-task-chat-agent.ts:60` reads `agent_type` from task; `persona-loader.ts:54-89` loads persona-specific prompts and learnings. Each persona gets isolated memory scope via `persona-loader.ts:80-86` | | |
| 14 | Tool use: web search | Supported | `agent/src/complex-task-chat/run-query.ts:135` includes `WebSearch` and `WebFetch` in base tool allowlist; `run-query.ts:224-226` renders search progress as status updates | | |
| 15 | Tool use: file read/write/edit | Supported | `agent/src/complex-task-chat/run-query.ts:135` includes `Read`, `Write`, `Edit`, `Glob`, `Grep` in base tool allowlist; agent operates in `/tmp/workspace` per `run-query.ts:101` | | |
| 16 | Tool use: shell/command execution | Supported | `agent/src/complex-task-chat/run-query.ts:135` includes `Bash` in base tool allowlist; `run-query.ts:228-229` renders shell progress | | |
| 17 | Artifact publishing | Supported | `agent/src/complex-task-chat/artifacts/port.ts:33-62` defines ArtifactStore with publish/fetch/list; `agent/src/complex-task-chat/artifacts/factory.ts:11-30` supports noop and S3 strategies; `agent/src/complex-task-chat/artifacts/s3-artifact-store.ts` uploads to S3 with presigned URLs; `complex-task-chat-agent.ts:123-130` provides scoped artifact tools per turn | | |
| 18 | Image understanding | Partial | `gateway/lambdas/ingest/channels/webchat.py:168-191` parses image attachments from webchat; `channels/base.py:60-70` defines MediaType.IMAGE; `sqs-client.ts:24` has `attachments` field in TaskPayload | Attachment URLs are parsed and forwarded, but `complex-task-chat-agent.ts` does not inject attachment content into the Bedrock/Claude prompt. The model never sees the image bytes. | S |
| 19 | Channel-aware response formatting | Supported | `agent/src/complex-task-chat/channel-profiles.ts:13-66` defines WEBCHAT_DIRECTIVE (4K char target, TL;DR lead) and SLACK_DIRECTIVE (mrkdwn, 3K char limit); `complex-task-chat-agent.ts:102-109` prepends channel directive to system prompt; `channel-profiles.ts:55-66` sets effort level per channel (webchat=medium) | | |
| 20 | Reconnection resilience | Supported | `gateway/lambdas/response/routers/websocket.py:68-106` resolves active connection_id from sessions table (not stale snapshot); `gateway/lambdas/ingest/handler.py:352-370` updates connection_id on each session access; `websocket.py:246-270` cleans up stale connections on GoneException | | |
| 21 | Cron-scheduled autonomous tasks | Missing | No cron/scheduler component exists in the agent-gateway path | Requires new scheduler service (EventBridge rules or similar) that injects synthetic messages into the ingest pipeline | M |
| 22 | Webhook-triggered agent tasks | Partial | `gateway/lambdas/ingest/handler.py:46-81` already handles HTTP events (not just WebSocket); REST-style invocation possible via API Gateway HTTP route | No dedicated webhook endpoint with signature verification for external systems. Current REST path requires Cognito auth. | S |
| 23 | Model failover and resilience | Partial | `agent/src/utils/resilientQuery.ts` wraps the Claude Agent SDK query with retry logic (exponential backoff, 3 retries); `run-query.ts:238-247` uses resilientQuery | No multi-model fallback chain. Single model per request. If Claude is down, retries the same model. No auth profile rotation. | M |
| 24 | Per-user session isolation (multi-tenant) | Supported | `gateway/lambdas/ingest/channels/webchat.py:135-136` requires Cognito sub as user identity; `complex-task-chat-agent.ts:80` calls `assertOwnership(session_id, user_id, tenant_id)` per task; `context/lcm/lcm-context.ts:121-160` throws if ownerUserId mismatches; `handler.py:164` keys sessions by thread_id or session_key | | |
| 25 | Thread-aware conversation management | Supported | `gateway/lambdas/ingest/handler.py:170-182` loads active threads per session; `classifier.py:51-67` classifies thread_action (new, follow_up, none); `handler.py:274-283` buffers messages for busy threads; `response/handler.py:148-209` manages per-thread re-enqueue after task completion | | |
| 26 | Message deduplication | Supported | `gateway/lambdas/ingest/handler.py:373-400` deduplicates identical messages within 5-second window; SQS FIFO `MessageDeduplicationId=task_id` at `handler.py:309` prevents replay; `sqs-client.ts:129` uses `resp_${task_id}` for response dedup | | |
| 27 | Follow-up message buffering | Supported | `gateway/lambdas/ingest/handler.py:274-283` detects busy thread and buffers follow-up; `response/handler.py:148-209` checks for buffered messages on task completion and re-enqueues; `handler.py:281` sends queued notification to user | | |
| 28 | Escalation to code tasks (GitHub issues) | Supported | `gateway/lambdas/ingest/github_dispatch.py:50-96` creates GitHub issue with enriched body and adds agent label; `classifier.py:59` routes `github_actions` path; `handler.py:223-268` handles dispatch including follow-up comments on existing issues | | |
| 29 | Voice interaction | Missing | No voice/audio processing in the agent-gateway path. No STT/TTS integration | Requires new voice channel adapter, Twilio/Telnyx integration, audio streaming infrastructure | L |
| 30 | Live Canvas: interactive visual workspace | Missing | No canvas/visual rendering in the agent-gateway path | Requires new frontend component and server-side rendering protocol. Fundamentally different architecture. | requires new module |
| 31 | Background memory consolidation ("Dreaming") | Missing | Memory is write-on-demand via `memory/tools.ts` (save_fact, save_learning). No background consolidation process | Requires scheduled background task that reviews recent sessions and promotes signals to long-term memory | M |
| 32 | Standing orders: autonomous recurring programs | Missing | No standing orders concept in the agent-gateway path | Requires new standing order definition format, cron integration, and approval-gated execution | L |
| 33 | Gmail/email integration | Missing | No email channel adapter | Requires new email adapter, IMAP/PubSub integration, email response formatting | M |
| 34 | Image generation | Missing | No image generation tool in the agent-gateway path. `run-query.ts:135` does not include any image generation tool | Requires new tool (e.g. DALL-E, Bedrock Stability) registered in the MCP tool server | S |
| 35 | Browser automation | Missing | No browser automation tool. `run-query.ts:135` has WebFetch (HTTP fetch) but not a headless browser | Requires Playwright/Puppeteer integration as an MCP tool | M |
| 36 | Delegation with tiered authorization | Partial | `classifier.py:57-59` classifies 3 execution paths with different authority levels (direct_response < long_running < github_actions). `github_dispatch.py` acts on behalf of users to create issues | No formal tier model, no approval gates, no configurable authority boundaries per agent | M |
| 37 | Multi-model provider support | Missing | `run-query.ts:100` hardcodes to single model (`global.anthropic.claude-sonnet-4-6`). `persona-loader.ts:44` allows `modelOverride` per persona, but all go through Bedrock | Single provider (Bedrock/Anthropic). No OpenAI, Google, or local model routing | L |
| 38 | Sandboxed tool execution | Partial | `run-query.ts:219` sets `permissionMode: 'bypassPermissions'` — no sandboxing. Agent runs in KEDA pod with IRSA role constraints at `serviceaccount.yaml` | Tool execution is not sandboxed; relies on IAM/IRSA for blast radius. No gVisor/Firecracker isolation. | M |
| 39 | Skills system | Supported | `run-query.ts:135` includes `Skill` in base tool allowlist; `run-query.ts:218` sets `settingSources: ['project']` for project-level skill loading; `complex-task-chat-agent.ts:221-247` renders skill progress | | |
| 40 | Large payload chunking | Supported | `gateway/lambdas/response/routers/websocket.py:48` sets `MAX_FRAME_BYTES=24*1024`; `websocket.py:167-220` splits content at UTF-8 boundaries into numbered chunks with `chunk_index/chunk_total`; backward-compatible (no chunk fields on small payloads) | | |
| 41 | OAuth authentication | Supported | `gateway/lambdas/ingest/handler.py:50-57` persists Cognito authorizer claims on $connect; `handler.py:94-157` restores claims on subsequent messages; `webchat.py:134-136` requires Cognito sub | | |
| 42 | Interactive message actions (Slack buttons) | Missing | `channels/slack.py` handles inbound messages only. No interactive reply handler, no Block Kit button/menu processing | Requires Slack interaction endpoint, action dispatch, ephemeral message support | M |
| 43 | Usage tracking and cost monitoring | Partial | `run-query.ts:296-305` harvests input/output token counts and cost from SDK result message; `sqs-client.ts:39` includes tokens in TaskResponse | Token counts are logged but not aggregated, stored, or queryable. No per-user/per-tenant cost dashboard | S |
| 44 | Heartbeat/keep-alive during long operations | Supported | `run-query.ts:29-33` defines `HEARTBEAT_INTERVAL_MS=20_000`; `run-query.ts:182-204` emits synthetic heartbeat when no real progress fires; `complex-task-chat-agent.ts:154-156` forwards heartbeat as "thinking..." progress frame; response Lambda delivers as WS frame to reset idle timeout | | |
| 45 | Session reset and lifecycle management | Partial | `gateway/lambdas/ingest/handler.py:357-370` sets session TTL (86400s = 24h); `agent/src/complex-task-chat/sweepers/session-sweeper.ts:46-66` cleans up expired sessions via DDB TTL stream; `context/lcm/lcm-context.ts:84` refreshes TTL on activity | No manual `/new` or `/reset` command. No configurable idle timeout. No daily reset at a specific time. | S |
| 46 | Media attachment handling | Partial | `channels/webchat.py:168-191` parses attachments with type/URL/filename; `channels/slack.py:307-337` parses Slack files with media type classification; `channels/base.py:73-123` defines MediaAttachment model; `sqs-client.ts:24` has `attachments` field | Attachments are parsed and forwarded in the SQS payload, but `complex-task-chat-agent.ts` does not inject them into the model prompt. Agent never sees attachment content. | S |
| 47 | Multi-step planning and research tasks | Supported | `run-query.ts:94-322` runs a full agent loop with up to 50 turns (`maxTurns=50`); tools include Bash, WebSearch, WebFetch, Read, Write, Edit; `run-query.ts:248-310` iterates assistant turns with tool_use blocks; `complex-task-chat-agent.ts:132-136` exposes context, memory, and artifact tools | | |
| 48 | Observability: OpenTelemetry tracing | Missing | No OpenTelemetry instrumentation in the agent-gateway path. Lambda logs only | Requires OTEL SDK integration in Lambda + chat-agent, collector deployment | M |
| 49 | iMessage integration | Missing | No iMessage adapter | Requires native macOS integration or BlueBubbles bridge; fundamentally different from server-side Lambda | requires new module |
| 50 | Nostr/Matrix/IRC federation | Missing | No federated protocol adapters | Requires new adapters per protocol with their own auth and delivery models | L |

---

## Summary

| Status | Count | Percentage |
|--------|-------|------------|
| Supported | 24 | 48% |
| Partial | 9 | 18% |
| Missing | 17 | 34% |
| **Total** | **50** | **100%** |

### Supported (24)

Core chat pipeline: #1 (multi-turn WS chat), #7 (classification/routing), #8 (FIFO serialization), #9 (progress indicators), #10 (context/summarization), #11 (cross-session memory), #12 (personas), #13 (multi-agent routing), #19 (channel-aware formatting), #20 (reconnection resilience), #24 (multi-tenant isolation), #25 (thread management), #26 (deduplication), #27 (follow-up buffering), #40 (large payload chunking), #41 (OAuth auth), #44 (heartbeat keep-alive).

Tool use: #14 (web search), #15 (file ops), #16 (shell exec), #17 (artifact publishing), #39 (skills), #47 (multi-step planning).

Escalation: #28 (GitHub issue dispatch).

### Partial (9)

| # | Use Case | Gap | Effort |
|---|----------|-----|--------|
| 2 | Slack bot | Missing Block Kit, interactive replies, streaming preview | M |
| 18 | Image understanding | Attachments parsed but not injected into prompt | S |
| 22 | Webhook-triggered tasks | REST path exists but no dedicated webhook endpoint | S |
| 23 | Model failover | Retry exists, no multi-model fallback | M |
| 36 | Tiered delegation | 3 paths exist, no formal tier/approval model | M |
| 38 | Sandboxed execution | IAM/IRSA only, no tool-level sandbox | M |
| 43 | Usage tracking | Tokens logged, not aggregated | S |
| 45 | Session lifecycle | TTL exists, no manual reset or configurable idle | S |
| 46 | Media attachments | Parsed, not injected into model | S |

### Missing (17)

| # | Use Case | Effort |
|---|----------|--------|
| 3 | Discord bot | M |
| 4 | Telegram bot | M |
| 5 | WhatsApp bot | L |
| 6 | Teams bot | L |
| 21 | Cron scheduled tasks | M |
| 29 | Voice interaction | L |
| 30 | Live Canvas | requires new module |
| 31 | Dreaming (memory consolidation) | M |
| 32 | Standing orders | L |
| 33 | Gmail/email | M |
| 34 | Image generation | S |
| 35 | Browser automation | M |
| 37 | Multi-model providers | L |
| 42 | Slack interactive actions | M |
| 48 | OpenTelemetry tracing | M |
| 49 | iMessage | requires new module |
| 50 | Nostr/Matrix/IRC | L |

### Top 3 Capability Gaps vs OpenClaw

1. **Channel breadth**: OpenClaw supports 24+ channels; we support 2 (webchat + Slack in partial). Discord, Telegram, WhatsApp, and Teams would cover the next most-requested channels. Each is an M-sized effort (new adapter + response router + channel config).

2. **Automation layer (cron/webhooks/standing orders)**: OpenClaw can run agents proactively on schedule, react to external events, and maintain long-running programs. We have zero automation — all agent work is user-initiated. Adding EventBridge-based scheduling + webhook endpoint would unlock use cases #21, #22, #32.

3. **Multi-model routing and failover**: OpenClaw routes to 15+ LLM providers with automatic failover, auth profile rotation, and cooldown logic. We're single-provider (Bedrock/Anthropic) with basic retry. This is a strategic gap given provider rate-limiting trends (HN: 1099-pt thread about Anthropic restrictions, 802-pt about Google restrictions).
