# Learnings from Issue #64 — Agent-gateway WebSocket E2E Suite + Ad-Hoc Scenarios

**Date**: 2026-04-20
**Agent**: @agent-developer
**Issue**: #64

## Key Technical Findings

### websockets v16 Breaking Changes
- `websockets.exceptions.InvalidStatusCode` → `websockets.exceptions.InvalidStatus` in v16. The old `exceptions` submodule is no longer a lazy-loaded alias.
- `ClientConnection.open` attribute removed. Use `ws.ping()` for liveness checks instead.
- `websockets.connect()` returns `ClientConnection` (not `WebSocketClientProtocol`).
- Pin to `websockets<16` in pyproject.toml if you need the old API, or update tests to use new API.

### Cognito M2M (client_credentials) Flow
- When a Cognito app client has a `ClientSecret`, the `/oauth2/token` endpoint requires `Authorization: Basic <base64(client_id:client_secret)>` header.
- Just sending `client_id` in the POST body without the secret returns HTTP 400.
- The Cognito domain is NOT always `<pool-name>.auth.<region>.amazoncognito.com` — it's whatever was configured. Use `aws cognito-idp describe-user-pool --query UserPool.Domain` to discover it.
- For the `bedrockgw-dev` pool, domain is `bedrockgw-dev-auth-18057152` (includes account suffix).
- The M2M token's `sub` equals the `client_id` (e.g., `378cm2jdj3rjt2os4cthub7267`).

### pytest addopts Override
- The pyproject.toml has `addopts = "-m 'not live_only and not workflow'"` which filters out all E2E tests by default.
- To run live tests, use `--override-ini="addopts="` to clear the default, then pass your own `-m` expression.
- Example: `uv run pytest tests/e2e/ --override-ini="addopts=" -m "live_only or kubectl"`

### ARC Runner Environment
- On ARC runners, AWS credentials come from IRSA (`adp-dev-agent-runner-role`), not `AWS_PROFILE=embark2`.
- The `embark2` profile doesn't exist in CI — just use default credentials.
- `uv` is not pre-installed — install with `pip install uv --break-system-packages`.
- kubectl needs kubeconfig setup: `aws eks update-kubeconfig --name adp-dev-eks-cluster --region us-east-1`

### WebSocket API Gateway Behavior
- **128KB frame limit**: API GW closes the connection with `1009 (message too big)` for frames >128KB. The message never reaches the Lambda integration.
- **Authorizer enforcement**: Missing token → 401. Empty/garbage token → 401/403. Wrong issuer → 403. All rejected before $connect Lambda.
- **$default route**: After $connect succeeds, subsequent frames on $default are NOT re-authenticated. Token expiry mid-session has no effect.
- **Response delivery**: The response Lambda uses `@connections` management API which does NOT check the original JWT. Stale connections get `GoneException`, logged as "is gone — cleaning up".

### KEDA ScaledJob Configuration
- The `agent-gateway-worker` ScaledJob still exists but the active worker is `chat-agent-worker`.
- KEDA auth uses `authentication` block with IRSA, not the deprecated `identityOwner` metadata field.
- Both ScaledJobs are present in `adp-gateway-agents` namespace.

### FIFO Queue Behavior
- `send_message` to FIFO queue MUST include `MessageGroupId` and `MessageDeduplicationId`.
- The ingest Lambda uses `session_id` as `MessageGroupId` for per-session serialization.
- Simple math questions are classified as `direct_response` (processed in-Lambda, not enqueued to FIFO).
- To test FIFO ordering, need complex prompts that trigger `long_running` classification.

### Ad-Hoc Scenario Execution Tips
- Use `asyncio.run(main())` with `websockets.connect()` for async WS testing.
- CloudWatch log search: `filter_log_events` with `filterPattern` is fast for targeted searches.
- DynamoDB sessions table uses `session_id` as partition key, scan with `FilterExpression` for connection lookups.
- Worker pods complete successfully even when the client disconnects (fire-and-forget pattern).

## Resource Reference
| Resource | Value |
|----------|-------|
| WebSocket API | `wss://8ea7pg40b7.execute-api.us-east-1.amazonaws.com/v1` |
| Cognito domain | `bedrockgw-dev-auth-18057152.auth.us-east-1.amazoncognito.com` |
| User pool | `us-east-1_JEhv9xSGG` |
| User client | `6cg7ba3hb4v41vbhm0cg8pl17j` |
| Agent client | `378cm2jdj3rjt2os4cthub7267` |
| FIFO tasks queue | `adp-dev-agent-gateway-tasks.fifo` |
| Sessions table | `adp-dev-agent-gateway-sessions` |
| Active ScaledJob | `chat-agent-worker` (not `agent-gateway-worker`) |
