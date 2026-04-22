# Issue #95 — OpenClaw Benchmark: Learnings

## Date: 2026-04-22
## Agent: @agent-developer
## Issue: research(agent-gateway): benchmark against OpenClaw

---

## What Worked

1. **OpenClaw's documentation is well-structured for research**: The `docs/concepts/` directory has 38+ files each covering a single architectural concept (memory, sessions, queue, streaming, etc.). This made systematic comparison efficient — map each concept to our codebase files.

2. **Our code comments citing OpenClaw were accurate**: The header comments in `channels/base.py`, `channels/webchat.py`, and `channels/slack.py` correctly describe which OpenClaw files they derived from. These were reliable starting points for tracing feature parity.

3. **HackerNews as a use-case source**: The top HN threads (1099, 802, 518, 514 points) revealed real user pain points (provider restrictions, security, cost) that translate directly into benchmarkable capability gaps (multi-model failover, sandboxing, usage tracking).

## Key Technical Findings

### Our Strengths vs OpenClaw
- **Thread-aware concurrency**: Our ingest handler has per-thread serialization, follow-up buffering, and re-enqueue after completion (`handler.py:271-315`, `response/handler.py:148-209`). OpenClaw's lane queue is similar but in-process only (single-node limitation).
- **LCM context management**: Our `context/lcm/` with compaction + summarization is more sophisticated than OpenClaw's built-in context engine. OpenClaw requires plugin engines for advanced compaction.
- **GitHub Actions escalation**: Our `github_dispatch.py` path (chat → issue → agent workflow) has no equivalent in OpenClaw. It's a differentiator for code-focused use cases.

### Our Gaps vs OpenClaw
- **Channel breadth**: 2 channels (webchat + partial Slack) vs 24+. The ChannelType enum defines 5 types but only 2 have adapters.
- **Automation**: Zero proactive/scheduled capability. OpenClaw has cron jobs, standing orders, heartbeat triggers, webhooks, and Gmail PubSub.
- **Multi-model routing**: Hardcoded to `global.anthropic.claude-sonnet-4-6` in `run-query.ts:100`. `persona-loader.ts:44` has `modelOverride` but still single-provider (Bedrock).

### Attachment Gap Detail
- `channels/webchat.py:168-191` parses attachments into `MediaAttachment` objects
- `channels/slack.py:307-337` does the same for Slack files
- `sqs-client.ts:24` has `attachments?: string[]` in `TaskPayload`
- BUT `complex-task-chat-agent.ts:55-71` destructures the task payload and never reads `attachments`
- The attachment URLs are in the SQS message but never injected into the Bedrock prompt
- Fix is S-sized: read `attachments` from task, convert to Claude image content blocks, prepend to `userMessage`

## Test Suite Design Decisions

1. **`ws_send_and_collect()` helper**: Centralized WS interaction with terminal detection (handles chunked + non-chunked responses, progress frames). More robust than raw `ws.recv()` in a loop.

2. **Terminal detection**: Combined check for `type=response` + `status=completed|failed|notification` + chunk reassembly. Learned from #85/#89 that the final frame structure varies.

3. **Skipped tests for missing use cases**: Used `@pytest.mark.skip(reason=...)` with the specific gap described. These serve as a live backlog — when a feature is implemented, remove the skip and the test name already maps to the use case.

4. **xfail for partial use cases**: `strict=False` so they don't red-fail CI but surface when gaps are closed. The `reason` string documents the exact gap.

## File Paths for Future Reference

| Component | Path |
|-----------|------|
| Use case catalog | `docs/openclaw-use-cases.md` |
| Fit assessment | `docs/openclaw-fit-assessment.md` |
| Test suite | `modules/agent-factory/tests/e2e/test_openclaw_parity.py` |
| Ingest handler | `modules/agent-factory/gateway/lambdas/ingest/handler.py` |
| Classifier | `modules/agent-factory/gateway/lambdas/ingest/classifier.py` |
| WebChat adapter | `modules/agent-factory/gateway/lambdas/ingest/channels/webchat.py` |
| Slack adapter | `modules/agent-factory/gateway/lambdas/ingest/channels/slack.py` |
| Chat agent worker | `modules/agent-factory/agent/src/complex-task-chat/complex-task-chat-agent.ts` |
| Run query (SDK loop) | `modules/agent-factory/agent/src/complex-task-chat/run-query.ts` |
| Channel profiles | `modules/agent-factory/agent/src/complex-task-chat/channel-profiles.ts` |
| LCM context | `modules/agent-factory/agent/src/complex-task-chat/context/lcm/lcm-context.ts` |
| Memory tools | `modules/agent-factory/agent/src/complex-task-chat/memory/tools.ts` |
| Artifact store | `modules/agent-factory/agent/src/complex-task-chat/artifacts/factory.ts` |
| WS response router | `modules/agent-factory/gateway/lambdas/response/routers/websocket.py` |
| Response handler | `modules/agent-factory/gateway/lambdas/response/handler.py` |

## Live Test Run Results (2026-04-22)

First run against dev: **12 passed, 5 xfailed, 19 skipped, 0 failures** in 131s.

### Latency baseline (supported tests)
- Fastest: UC07 (greeting classification) — 3.29s
- Slowest: UC44 (heartbeat/research) — 11.48s
- Median: 8.25s, Mean: 8.59s, P90: 11.17s

### Test fixes applied during live run
1. **UC40 (chunking)**: Classifier routes short essay requests as `direct_response` (placeholder reply). Fix: use research-style prompts with "use web search" to ensure `long_running` routing.
2. **UC18 (image understanding)**: Model inferred "cat" from the URL (`Cat03.jpg`), not from actual image bytes. Fix: use neutral filename `upload.bin` and require detailed visual description.
3. **UC45 (session reset)**: Model responded conversationally to `/new` without actually clearing context. Fix: verify pre-reset context is gone post-reset.
4. **UC46 (media attachments)**: Model acknowledged "a document was mentioned" without seeing it. Fix: require specific document content in the reply.

### Environment configuration for live runs
```bash
export TEST_ENV=dev
export WS_URL="wss://8ea7pg40b7.execute-api.us-east-1.amazonaws.com/v1"
export TASKS_QUEUE_URL="https://sqs.us-east-1.amazonaws.com/879318057152/adp-dev-agent-gateway-tasks.fifo"
export RESPONSES_QUEUE_URL="https://sqs.us-east-1.amazonaws.com/879318057152/adp-dev-agent-gateway-responses"
export COGNITO_USER_POOL_ID="us-east-1_JEhv9xSGG"
export COGNITO_CLIENT_ID="6cg7ba3hb4v41vbhm0cg8pl17j"
export TEST_USER_EMAIL="adp-test@example.com"
export RUN_COSTLY_TESTS=1
# TEST_USER_PASSWORD must be set separately (not stored in learnings)
```

### Key lesson: LLM tests need anti-guessing assertions
When testing LLM capabilities, the model often "passes" tests by inferring answers from context clues (filenames, URLs, prompt wording) rather than using the actual feature. Always design assertions that require the real capability — neutral filenames, multi-step verification (set context → action → verify context changed), and require specific content the model can only know if the feature works.

## Recommendations

1. **Quick wins (S-sized)**: Inject attachments into prompt (#18/#46), add session `/reset` command (#45), expose token usage in response frames (#43).
2. **Medium wins**: Finish Slack delivery (Block Kit, interactive), add Discord adapter, add webhook endpoint.
3. **Strategic**: Multi-model routing + failover is the single most impactful gap given provider restriction trends.
