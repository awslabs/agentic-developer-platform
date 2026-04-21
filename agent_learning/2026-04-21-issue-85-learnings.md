# Issue #85 Learnings: WS chunking, channel-aware prompts, client termination

**Date**: 2026-04-21
**Agent**: @agent-developer
**Issue**: fix(agent-gateway): chunk large WS frames, channel-aware prompts, client termination

## What worked

### Problem A — Frame chunking
- API Gateway WebSocket has a 128 KB hard limit, but frames >32 KB can be silently dropped due to permessage-deflate fragmentation. A 27 KB JSON-wrapped frame was enough to trigger this.
- Conservative 24 KB threshold with 512 bytes reserved for envelope overhead is safe. The envelope (type, task_id, timestamp, chunk_index, chunk_total) is typically ~200-300 bytes.
- UTF-8 boundary-aware splitting is essential — naively cutting at byte offsets can split multi-byte characters and produce invalid UTF-8. Walk back from the target split point to find a valid boundary (check continuation byte mask `0xC0 == 0x80`).
- Extracted `_send_frame()` helper from `route()` to avoid code duplication between single-frame and chunked paths.

### Problem B — Channel-aware prompts
- Channel directives compose on top of persona prompts, not replace them. Prepending the directive before the base persona keeps persona voice intact while adding channel constraints.
- `getChannelMaxTokens()` is separate from `getChannelDirective()` to keep concerns cleanly separated — one shapes the prompt, the other caps the model output.
- The `maxTokens` field in `runQuery` streamOptions only gets set when defined (not `undefined`), preserving existing SDK-default behavior for channels without a cap.

### Problem C — Client terminal frame detection
- The WS router emits `{type: "response", content: "..."}` without a `status` field for the final reply. The original client checked `status in ("completed", "failed")` which never matched. Fixed by also treating `type=response` with non-empty content as terminal.
- Chunk reassembly in the client: buffer chunks by task_id, only consider the response terminal when `chunk_index == chunk_total`.
- The `ws_roundtrip.py` script needs `websockets` as a runtime dependency — it's imported lazily inside the roundtrip function.

## Key decisions
- 24 KB chunk threshold (not 32 KB or higher) — conservative, avoids the permessage-deflate edge case entirely
- Channel profiles as a separate file (`channel-profiles.ts`) rather than inline in the agent — makes it easy to add WhatsApp/SMS later
- `ws_roundtrip.py` placed in `scripts/` (not `tests/`) — it's a user-facing diagnostic tool, not a test fixture

## File locations
- WebSocket router with chunking: `modules/agent-factory/gateway/lambdas/response/routers/websocket.py`
- Channel profiles: `modules/agent-factory/agent/src/complex-task-chat/channel-profiles.ts`
- Channel profiles tests: `modules/agent-factory/agent/src/complex-task-chat/channel-profiles.test.ts`
- WS roundtrip script: `modules/agent-factory/scripts/ws_roundtrip.py`
- Chunking unit tests: `modules/agent-factory/tests/lambda/test_websocket_router.py` (TestFrameChunking class)
- Client termination tests: `modules/agent-factory/tests/lambda/test_ws_roundtrip_logic.py`

## Gotchas
- `moto` mock for DynamoDB is needed for the existing WebSocket router tests. The new chunking tests don't need DynamoDB since they test the content-splitting logic with `sessions_table=None`.
- pytest-asyncio is required for the ws_roundtrip logic tests since `roundtrip()` is async.
- When re-importing modules in tests (to get fresh module state), use `importlib.reload()` and be careful about `sys.modules` cleanup.
