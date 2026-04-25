# Issue #130 — Playwright E2E Chat Regression Suite

**Date**: 2026-04-24
**Agent**: @agent-operations
**Status**: Complete (PR #131)

## What Was Done

Built a Playwright-based E2E regression suite at `tests/e2e/chat/` covering 19 chat-UX scenarios across 7 test modules, plus a GitHub Actions workflow for CI.

## Key Technical Decisions

### Test Structure
- Placed tests at `tests/e2e/chat/` (repo root) rather than inside `modules/gateway/tests/e2e/` because the suite spans multiple modules (gateway frontend, agent-factory lambdas, agent-factory worker). The root `tests/` location better reflects its cross-cutting nature.
- Used `pytest.mark.parametrize` for routing test cases (scenarios 4-9) — cleaner than 6 separate test methods with identical logic.

### Authentication Strategy
- Used **programmatic token injection** (Cognito `admin-initiate-auth` → inject into sessionStorage) rather than driving the hosted-UI flow for speed. The hosted-UI flow is already tested in `modules/gateway/tests/e2e/test_frontend_smoke.py`.
- Session-scoped `cognito_tokens` fixture avoids repeated auth calls across tests.

### CloudWatch Log Correlation
- The ingest Lambda logs `Route: path=<route>` which we poll with `filter-log-events`. This is the most reliable way to verify routing decisions without modifying the Lambda code.
- Used a 120s lookback window + 60s poll timeout. Production log delivery can lag 5-15s.

### Guard Gate: `E2E_CHAT_ENABLED`
- Tests are auto-skipped unless `E2E_CHAT_ENABLED=1` to prevent accidental runs that hit live infra.

## Gotchas and Things That Took Effort

1. **Playwright sync vs async API**: Used sync API (`sync_playwright`) because pytest-playwright's async support adds complexity without benefit here. The sync API is simpler for UI-driven tests.

2. **CDP WebSocket observation** (scenario 2): You need `context.new_cdp_session(page)` + `Network.enable` BEFORE navigating to capture the WS creation event. Order matters.

3. **localStorage vs sessionStorage**: Cognito tokens go in `sessionStorage`, conversations go in `localStorage` (key: `adp_chat_conversations`). These are different storage mechanisms — don't confuse them.

4. **Refusal phrase matching**: Simple substring match is sufficient. The phrases are short and distinctive enough not to cause false positives.

5. **xfail for scenario 19**: Used `pytest.mark.xfail(strict=False)` so the test doesn't fail the suite but documents the known limitation. When the fix lands, it'll start passing (xpass) which is visible in CI but non-blocking.

## Useful Patterns

- **Auto-screenshot on failure**: The `pytest_runtest_makereport` hook in conftest captures screenshots when any test fails — invaluable for CI debugging.
- **Parameterized routing tests**: `pytest.param(..., id="scenario6-...")` gives readable test IDs in output.
- **Helpers module**: Keeping all Playwright interaction helpers in `helpers.py` (not conftest) allows them to be imported and used in ad-hoc scripts too.

## Resource References

- CloudFront URL: `https://d1g6cal2ts4iis.cloudfront.net`
- Secrets Manager: `adp/dev/gateway/test-admin-credentials`
- Ingest Lambda log group: `/aws/lambda/adp-dev-agent-gateway-ingest`
- localStorage key: `adp_chat_conversations`
- sessionStorage keys: `cognito_id_token`, `cognito_access_token`, `cognito_refresh_token`
- Cognito pool: `us-east-1_JEhv9xSGG`, client: `6cg7ba3hb4v41vbhm0cg8pl17j`

## Existing Test Suites (context for future agents)

| Suite | Location | Focus |
|-------|----------|-------|
| Gateway E2E (API) | `modules/gateway/tests/e2e/` | REST API, admin, auth, budget, pools |
| Agent Factory E2E | `modules/agent-factory/tests/e2e/` | WS roundtrip, behavior, OpenClaw parity |
| Chat Playwright | `tests/e2e/chat/` (this PR) | Browser-level chat UX regressions |
| Frontend smoke | `modules/gateway/tests/e2e/test_frontend_smoke.py` | Basic page load + Cognito redirect |
