# Issue #68 Learnings — WS delivery, heartbeat, dedupe

**Date**: 2026-04-20
**Agent**: @agent-developer
**PR**: #69

## Bug 1: Stale connection_id delivery

### What worked
- The sessions table already had the authoritative `connection_id` (written by ingest Lambda on every `get_or_create_session`). The fix was read-side only — add a DynamoDB `GetItem` in the response router before `post_to_connection`.
- Using `ConsistentRead=True` on the session lookup prevents stale reads after a rapid disconnect/reconnect cycle.
- The `ConditionExpression="connection_id = :stale"` on cleanup prevents clobbering a concurrent reconnect. This is important — without it, the cleanup from a GoneException on the OLD connection would nuke the NEW connection_id that was just written by a reconnect.

### Key decision
- Session lookup happens on every `route()` call, not just final replies. This means progress frames also go to the active connection, which is the right behavior — if someone reconnects mid-turn, they should see heartbeats too.

### Gotcha
- `_cleanup_connection` now takes `session_id` as a parameter (breaking the old signature). The response handler already passes `session_id` in metadata, but if any other code calls `_cleanup_connection` directly with just `connection_id`, it would silently no-op. The old code was a TODO stub anyway.

## Bug 2: Heartbeat during pure-reasoning

### What worked
- Converting the existing `setInterval` heartbeat from log-only to `emitProgress` with `force=true` (bypasses the `PROGRESS_MIN_INTERVAL_MS` throttle since the heartbeat interval is already longer).
- Coalescing logic: `if (Date.now() - lastProgressAt < HEARTBEAT_INTERVAL_MS) skip` — if a real progress event fired recently, the heartbeat is redundant.

### Key decisions
- Heartbeat interval: 20s (down from 30s log-only). API Gateway WebSocket idle timeout is 10 min, so 20s gives ~30 frames before timeout. Could go lower but diminishing returns.
- FIFO dedup ID for progress now includes `Date.now()` — heartbeats can fire multiple times per turn on the same kind, so the old `prog_{task}_{turn}_{kind}` wasn't unique enough. Using timestamps is fine since SQS FIFO dedup window is 5 minutes and heartbeats are 20s apart.

### Gotcha
- The `emitProgress` function's `force` parameter bypasses BOTH the time throttle and the key-match dedup. This is intentional for heartbeats (they're all `key="heartbeat"` so the dedup would block all but the first), but be careful if `force` is used for other event types.

## Bug 3: Duplicate and empty messages

### What worked
- Empty/whitespace guard at the top of `append_message` with early return. Simple and catches both the empty-string and None cases.
- Dedupe via DynamoDB `GetItem` + compare last message. The 5-second window is generous enough to catch rapid-fire duplicates but won't block legitimate repeated messages (e.g., user sends "yes" twice, 10 seconds apart).

### Root cause confirmed
- The double-ack came from `handle_long_running` calling `send_notification` (which calls `append_message`) with the escalation_note. For `direct_response` path, `handle_direct_response` calls `append_message` with `classification.response`. These are mutually exclusive paths, so the "two call sites" described in the issue is actually about the escalation_note being appended alongside the user message. The dedupe guard catches the edge case where `send_notification` gets called with identical content to a previous append.

### Performance note
- The dedupe adds a DynamoDB `GetItem` on every `append_message` call (to check the last message). This is ~2ms at P99 for a single-key read with eventual consistency. Acceptable since `append_message` is called at most 2-3 times per request. If this becomes a hot path, consider caching the last message in-memory within the Lambda invocation.

## Test patterns
- `moto.mock_aws` as a class-level decorator (`@mock_aws`) on individual test methods works cleanly for the router tests where each test needs its own DynamoDB table.
- For the ingest handler tests, the `mocked_aws_services` fixture wrapping `mock_aws` as a context manager is better since it shares the SQS queue setup.
- Fresh module import (`_import_handler()` / `_import_router()`) with `del sys.modules[...]` is essential — the Lambda handlers cache boto3 clients at module level.
