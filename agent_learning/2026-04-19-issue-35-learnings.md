# Learnings from Issue #35 — Re-execute Bedrock gateway E2E suite (v2)

**Date:** 2026-04-19
**Agent:** @agent-operations
**Issue:** #35
**Outcome:** 97/97 pytest passed, 10/11 bash e2e_test.sh passed

## Key Technical Details

### Environment Endpoints
- **REST API Gateway:** `https://59o2rakc50.execute-api.us-east-1.amazonaws.com/dev` — used by `api_client` fixture
- **CloudFront:** `https://d1g6cal2ts4iis.cloudfront.net/` — used by `cloudfront_client` fixture and `e2e_test.sh`
- **Cognito pool:** `us-east-1_JEhv9xSGG` (`bedrockgw-dev-users`)
- **PKCE client:** `6cg7ba3hb4v41vbhm0cg8pl17j`
- **Agent client:** `378cm2jdj3rjt2os4cthub7267`
- **Test user:** `adp-test@example.com` (password must be re-set each run — not persisted)

### Test User Password Management
- The test user password is NOT stored anywhere persistent (no SSM, no Secrets Manager)
- Each run needs to `aws cognito-idp admin-set-user-password` to set a known password
- Use a password with uppercase, lowercase, numbers, and special chars to meet Cognito policy
- AVOID `!` in passwords when they'll be used in bash scripts — history expansion can cause issues even with proper quoting

### pytest vs e2e_test.sh Differences
- **pytest** uses `API_GATEWAY_URL` (REST API Gateway) via `api_client` fixture — bypasses CloudFront entirely
- **e2e_test.sh** uses `CLOUDFRONT_URL` — goes through CloudFront to ALB to backend
- The two paths can produce different results because:
  - API Gateway has its own Lambda authorizer that may reject tokens differently
  - CloudFront hits the backend directly, exposing backend errors (like the admin 500)
  - The API Gateway authorizer returns 403 for user-pool JWTs on admin routes

### `uv` for Test Execution
- The runner doesn't have `pip` — use `uv` instead
- Install: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Then: `uv sync --extra dev` and `uv run pytest ...`
- PATH needs `$HOME/.local/bin` for `uv`

### Bedrock CLI Invocation
- `aws bedrock-runtime invoke-model --body` with inline JSON fails with "Invalid base64" error
- Must use `fileb://` prefix: write JSON to file, then `--body fileb:///tmp/body.json`

### Findings
- `/api/admin/organizations` returns HTTP 500 from uvicorn when accessed via CloudFront — this is a backend bug, not a routing issue
- The same endpoint returns 403 via API Gateway (authorizer rejects user-pool JWT for admin routes)
- This means the admin endpoints have two separate issues: backend crash AND authorizer config

## What Worked Well
- The fixture fix (#32) completely resolved the HTML-200 masking issue from #30
- The IRSA/secrets fix (#34) resolved all auth and proxy failures
- All 6 API test categories went from mixed results to 97/97 green

## Recommendations
1. Store the test user password in SSM or Secrets Manager to avoid re-setting each run
2. Fix the admin organizations backend endpoint (check logs for traceback)
3. Consider updating e2e_test.sh to also support API Gateway URL as an alternative to CloudFront
4. The `e2e_test.sh` human auth flow has transient failures — consider adding a retry
