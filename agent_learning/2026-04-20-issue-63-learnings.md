# Learnings: Issue #63 — Gateway E2E Suite Execution (post-#62)

## Date: 2026-04-20
## Agent: @agent-operations
## Issue: #63

## Context
Ran the full gateway E2E test suite against the live deployed environment. PR #62 (RBAC fix) was not yet merged at time of execution.

## Results
- Pytest: 95/96 passed, 1 failed (known RBAC bug, fix in PR #62)
- Bash CLI: 11/11 passed
- All pre-flight checks passed

## Key Learnings

### 1. AWS_PROFILE=embark2 not available in CI
The issue specifies `AWS_PROFILE=embark2` but this profile is not available in the GitHub Actions runner environment. The runner already has credentials via IRSA (`adp-dev-agent-runner-role`), so no profile is needed. Future agents should use default credentials in CI.

### 2. report.py mode classification is heuristic-based
The `tests/scripts/report.py` classifies tests as "live", "integration", or "unit" based on class/test naming patterns (e.g., "Live", "HTTP", "RBAC"), NOT by actual runtime mode. When running with `TEST_ENV=dev`, ALL tests hit the live gateway, but report.py still shows "0 live tests" for most categories. This is a labeling artifact, not a real coverage gap. Don't be alarmed by the "0 live tests" warnings.

### 3. Pre-#62 credential setup
Before PR #62 merges, the E2E harness does NOT auto-discover credentials from Secrets Manager. You must manually export:
- `TEST_USER_EMAIL`, `TEST_USER_PASSWORD` (from `adp/dev/gateway/test-user-credentials`)
- `API_GATEWAY_URL=https://59o2rakc50.execute-api.us-east-1.amazonaws.com/dev`
- `CLOUDFRONT_DOMAIN=d1g6cal2ts4iis.cloudfront.net`
- `COGNITO_USER_POOL_ID=us-east-1_JEhv9xSGG`
- `COGNITO_CLIENT_ID=6cg7ba3hb4v41vbhm0cg8pl17j`
- `TEST_ENV=dev`

### 4. The single failure is the known RBAC bug
`test_non_admin_cannot_list_users` fails because non-admin users get HTTP 200 with empty data on `/admin/organizations/org-test/users` instead of 403. This is exactly what PR #62 fixes in `modules/gateway/src/admin/access_control.py`. Not a regression.

### 5. Bash CLI e2e_test.sh admin endpoint check
The bash script checks admin endpoint and accepts both 200 and 403 as valid (line 446), so it passes even with the RBAC bug. This is intentional — the script treats both as "admin endpoint accessible."

### 6. uv needs to be installed in CI
The CI runner doesn't have `uv` pre-installed. Install with `pip install --break-system-packages uv`, then use `uv sync --extra dev` and `uv run pytest`.

### 7. Cognito app clients
- `bedrockgw-dev-client` (6cg7ba3hb4v41vbhm0cg8pl17j) — human user auth (ADMIN_USER_PASSWORD_AUTH)
- `bedrockgw-dev-agent-client` (378cm2jdj3rjt2os4cthub7267) — M2M agent auth (client_credentials)

### 8. Test execution time
The full 96-test pytest suite completes in ~5 seconds. The bash CLI flow takes ~30 seconds (includes actual Bedrock calls). Combined run is well under 1 minute.

## Endpoints
- REST API: `https://59o2rakc50.execute-api.us-east-1.amazonaws.com/dev`
- CloudFront: `https://d1g6cal2ts4iis.cloudfront.net`
- Cognito pool: `us-east-1_JEhv9xSGG`
- Cognito domain: `bedrockgw-dev-auth-18057152.auth.us-east-1.amazoncognito.com`
