# E2E Chat Playwright Regression Suite

Playwright-based end-to-end regression tests for the ADP chat UX. Runs against the live dev CloudFront URL and catches chat regressions that unit tests cannot.

## Scenarios Covered

| # | File | Scenario | Regression of |
|---|------|----------|---------------|
| 1 | `test_auth.py` | Login round-trip (tokens in sessionStorage) | - |
| 2 | `test_auth.py` | WebSocket opens after login (CDP observation) | - |
| 3 | `test_auth.py` | No CSP refusal on WS connect | #117 |
| 4-9 | `test_routing.py` | Classifier routing (direct_response / long_running) | #118, #129 |
| 10 | `test_routing.py` | Refusal-phrase check on long_running replies | #129 |
| 11 | `test_history.py` | No topic bleed across turns | #121, #129 |
| 12 | `test_history.py` | Agent doesn't re-run prior work | #121 |
| 13 | `test_bash.py` | Bash actually runs | #119, #120 |
| 14 | `test_bash.py` | Per-tool-call logs emitted | #119 |
| 15 | `test_persistence.py` | Messages persist to localStorage | #122, #123 |
| 16 | `test_persistence.py` | Multi-conversation independence | #122 |
| 17 | `test_persistence.py` | Survives page reload | #122, #123 |
| 18 | `test_ack.py` | Long-running ACK visible within 3s | #119 |
| 19 | `test_known_issues.py` | Durability flag (xfail) | #124 successor |

## Running Locally

### Prerequisites

```bash
pip install pytest playwright boto3
playwright install chromium
```

### Against dev environment

```bash
# With AWS credentials configured (profile or env vars):
AWS_PROFILE=adp-dev E2E_CHAT_ENABLED=1 python -m pytest tests/e2e/chat/ -v

# Override the CloudFront URL:
E2E_CLOUDFRONT_URL=https://your-cf-domain.cloudfront.net \
  E2E_CHAT_ENABLED=1 \
  python -m pytest tests/e2e/chat/ -v

# Run a specific scenario:
E2E_CHAT_ENABLED=1 python -m pytest tests/e2e/chat/test_routing.py -v -k "scenario6"
```

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `E2E_CHAT_ENABLED` | Yes | `0` | Must be `1` to enable tests |
| `E2E_CLOUDFRONT_URL` | No | `https://d1g6cal2ts4iis.cloudfront.net` | CloudFront URL to test |
| `AWS_REGION` | No | `us-east-1` | AWS region |
| `ENVIRONMENT` | No | `dev` | Environment name |
| `E2E_TEST_USERNAME` | No | (from Secrets Manager) | Override test user email |
| `E2E_TEST_PASSWORD` | No | (from Secrets Manager) | Override test user password |
| `COGNITO_USER_POOL_ID` | No | (from Secrets Manager) | Override Cognito pool ID |
| `COGNITO_CLIENT_ID` | No | (from Secrets Manager) | Override Cognito client ID |

### Credentials

Tests fetch credentials from AWS Secrets Manager at runtime:
- Secret: `adp/dev/gateway/test-admin-credentials`
- Contains: `username`, `password`, `cognito_user_pool_id`, `cognito_client_id`

You can override with environment variables (see above).

### CloudWatch Access

Routing tests (scenarios 4-10) and tool-log tests (scenario 14) read CloudWatch logs:
- Log group: `/aws/lambda/adp-dev-agent-gateway-ingest`
- Required IAM: `logs:FilterLogEvents` on the log group

## CI Workflow

The workflow runs on:
- **Push to main** (when chat-related paths change)
- **Nightly** (03:00 UTC schedule)
- **Manual dispatch** (`workflow_dispatch`)

See `.github/workflows/e2e-chat-playwright.yml`.

## Debugging Failures

Each failed test automatically captures:
1. **Screenshot** — saved to `/tmp/e2e-chat-*.png` and uploaded as CI artifact
2. **Assertion message** — includes the expected route, actual route, and relevant context
3. **CloudWatch log excerpts** — for routing tests, the last 50 lines of the ingest Lambda log

To see failure details in CI, check the `e2e-chat-results` artifact.

## Architecture

```
tests/e2e/chat/
  conftest.py       # Fixtures: credentials, browser, authenticated page, CDP
  helpers.py        # Utilities: login, CW polling, localStorage, chat interaction
  test_auth.py      # Scenarios 1-3: auth + WS lifecycle
  test_routing.py   # Scenarios 4-10: classifier routing + refusal checks
  test_history.py   # Scenarios 11-12: history framing
  test_bash.py      # Scenarios 13-14: bash execution + tool logs
  test_persistence.py  # Scenarios 15-17: localStorage persistence
  test_ack.py       # Scenario 18: immediate acknowledgement
  test_known_issues.py # Scenario 19: durability flag (xfail)
```
