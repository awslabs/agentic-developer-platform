# Issue #93 Learnings — E2E behavior regression tests

## Date: 2026-04-22
## Agent: @agent-developer

## What Was Done
Added 8 live E2E regression tests covering fixes from #68/#85/#87/#88/#89/#92,
plus a latency recording harness that produces per-test JSON and a summary table.

## Key Technical Decisions

### Test structure
- Kept all new tests in a single `test_chat_agent_behavior.py` (one class per gap)
  rather than splitting per-fix. This keeps the test file scannable and the
  `make test-e2e-regression` target simple.
- Used `@pytest.mark.costs_money` + `RUN_COSTLY_TESTS=yes` gating to prevent
  accidental Bedrock costs in CI.

### AsyncWSClient with chunk reassembly
- Created `AsyncWSClient` in conftest.py that wraps `websockets.connect` and
  handles the chunk reassembly contract (chunk_index/chunk_total) inline.
- `recv_until_terminal()` waits for `status=completed|failed` — matches the
  terminal detection logic from `scripts/ws_roundtrip.py`.
- This is more ergonomic than raw websockets for multi-frame assertions.

### Latency harness
- `LatencyRecorder` uses `time.monotonic()` marks, not wall-clock. Deltas are
  meaningful even if NTP adjusts the clock during a test.
- Each recorder writes its own JSON to `/tmp/e2e-latency-<test>.json`.
- Session-scoped `pytest_sessionfinish` hook aggregates and prints a table.
- No latency thresholds that fail tests — just recording for baseline.

### Cleanup fixture
- `cleanup.track(session_id)` collects IDs during the test.
- After test, deletes from both `adp-dev-agent-gateway-sessions` and
  `adp-dev-chat-context` (PK=session#<id>, all SK rows via query+batch-delete).
- Skipped for `@pytest.mark.leave_data` tests.

## Gotchas

1. **pytest-asyncio version**: The installed version (1.3.0) uses `asyncio_mode = "auto"`
   from pyproject.toml. Tests must be `async def` and marked `@pytest.mark.asyncio`.

2. **pytest marker deselection**: pyproject.toml has `addopts = "-m 'not live_only...'"`.
   The `make test-e2e-regression` target overrides this with `-m "live_only and costs_money"`.

3. **Token sub extraction**: To verify ownerUserId == Cognito sub, we decode the JWT
   payload with base64 (no verification needed — just reading the claim).
   Must add padding: `parts[1] + "=" * (4 - len(parts[1]) % 4)`.

4. **DynamoDB chat-context schema**: PK = `session#<sessionId>`, SK = `header` | `msg#<id>` |
   `item#<ordinal>` | `summary#<id>`. This is the schema from dynamo-store.ts.

5. **Sessions table key**: `session_id` is the hash key (no range key). Connection claims
   use `session_id = conn#<connectionId>`.

## Files Created/Modified
- `tests/e2e/latency.py` — LatencyRecorder + session hooks
- `tests/e2e/test_chat_agent_behavior.py` — 8 test cases
- `tests/conftest.py` — new fixtures (fresh_jwt, ws_client_async, chat_context_row,
  sessions_row, cleanup, latency_recorder) + costs_money marker handling
- `pyproject.toml` — 3 new markers
- `Makefile` — test-e2e-regression target

## Resource Names
- Sessions table: `adp-dev-agent-gateway-sessions`
- Context table: `adp-dev-chat-context`
- Test creds secret: `adp/dev/gateway/test-user-credentials`
- FIFO queue: `adp-dev-agent-gateway-tasks.fifo`
- AWS profile: `embark2` (account 879318057152, us-east-1)
