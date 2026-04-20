# BedrockGateway Tests

This directory contains the test suite for BedrockGateway, including unit tests, integration tests, and end-to-end (E2E) tests.

## Directory Structure

```
tests/
├── conftest.py              # Shared pytest configuration and fixtures
├── fixtures/                # Test fixtures and utilities
│   ├── __init__.py
│   ├── factories.py         # Factory functions for creating test entities
│   ├── mock_aws.py          # Mock AWS service clients (STS, Bedrock)
│   └── seed_data.py         # Test data seeding utilities
├── integration/             # Integration tests (cross-unit interactions)
│   ├── __init__.py
│   ├── test_auth_proxy_flow.py      # Auth -> Token -> Proxy flow
│   ├── test_budget_enforcement.py   # Budget check -> Enforcement -> Usage
│   ├── test_ratelimit_headers.py    # Rate limit -> Enforcement -> Headers
│   ├── test_pool_failover.py        # Pool round-robin -> Failover
│   └── test_middleware_chain.py     # Middleware execution order
├── e2e/                     # End-to-end tests (user story scenarios)
│   ├── __init__.py
│   ├── config.py            # LiveTestConfig with env-var-first, SSM-fallback
│   ├── conftest.py          # Dual-mode fixtures (api_client, iam_signed_client, JWT helpers)
│   ├── test_authentication_stories.py  # US-1.4, US-1.5, US-1.6
│   ├── test_proxy_stories.py           # US-4.1, US-4.2, US-4.3
│   ├── test_budget_stories.py          # US-2.1, US-2.2, US-2.3, US-2.4
│   ├── test_ratelimit_stories.py       # US-3.1, US-3.2, US-3.3
│   ├── test_pool_stories.py            # US-1.2, US-5.1
│   ├── test_admin_stories.py           # US-7.x, US-8.x
│   └── test_frontend_smoke.py          # Browser/Playwright tests
├── auth/                    # Unit tests for auth module
├── proxy/                   # Unit tests for proxy module
├── budget/                  # Unit tests for budget module
├── ratelimit/               # Unit tests for rate limit module
├── pool/                    # Unit tests for pool module
├── admin/                   # Unit tests for admin module
├── usage/                   # Unit tests for usage module
└── cli/                     # Unit tests for CLI tools
```

## E2E Test Architecture

### What lives in `tests/e2e/`

The `e2e/` directory contains a **mix** of three test types:

| Marker | What it does | Example |
|--------|-------------|---------|
| `@pytest.mark.unit` | Tests pure Python logic with `db_session` + mocks. No HTTP calls. | `TestHumanUserAuthentication.test_exchange_valid_aws_credentials_returns_token` |
| `@pytest.mark.integration` | Exercises the FastAPI ASGI app via `api_client` (in-process HTTP). Dual-mode: hits ASGI in unit, real HTTP in live. | `TestHTTPAuthFlows.test_unauthenticated_request_returns_401_or_403` |
| `@pytest.mark.live_only` | Makes real HTTP calls to the deployed gateway. Zero value in unit mode; auto-skipped. | `TestLiveOAuthAuth.test_valid_user_jwt_gets_200_on_health` |

Running `pytest tests/e2e/` in **unit mode** (default) executes all `unit` and `integration` tests against the ASGI app. `live_only` tests are automatically skipped.

Running in **live mode** (`TEST_ENV=dev`) additionally enables `live_only` tests that hit the deployed REST API Gateway.

### Which endpoint gets hit

**Both humans and agents use the REST API Gateway endpoint** (`API_GATEWAY_URL`). CloudFront is only relevant for the SPA and `/api/*` convenience routing.

| Fixture | Target | Use for |
|---------|--------|---------|
| `api_client` | REST API Gateway (`API_GATEWAY_URL`) | All API contract tests (auth, proxy, admin, budget, ratelimit, pool) |
| `iam_signed_client` | REST API Gateway (`API_GATEWAY_URL`) with SigV4 | IAM-auth tests (agent path) |
| `cloudfront_client` | CloudFront (`CLOUDFRONT_DOMAIN`) | Frontend smoke tests, CDN-layer checks only |

**Do not use CloudFront for API contract assertions.** CloudFront routes `/*` to the S3 origin serving the SPA; unknown URLs under `/` return `200 index.html`, masking real backend 4xx/5xx responses.

### Two authentication modes

The REST API Gateway supports **two authentication modes**, both covered by the test suite:

#### 1. OAuth / Cognito JWT

- Request carries `Authorization: Bearer <jwt>`
- Token obtained via `ADMIN_USER_PASSWORD_AUTH` (test user) or `client_credentials` grant (M2M agent)
- Lambda authorizer validates JWT, extracts claims, injects identity context
- **Fixtures**: `jwt_for_user`, `jwt_for_admin`, `jwt_for_agent`, `expired_jwt`, `malformed_jwt`
- **Used by**: web UI, CLI clients, agents using Cognito

#### 2. IAM SigV4

- Request signed with AWS IAM credentials (SigV4) -- no JWT
- API Gateway `aws_iam` auth method validates the signature
- Backend resolves the caller to an org/team via `X-Auth-Source: iam` + `X-Agent-*` headers
- **Fixture**: `iam_signed_client`
  - **Unit mode**: Adds fake SigV4 headers (no real AWS creds needed)
  - **Live mode**: Uses `botocore.auth.SigV4Auth` with real credentials from the environment
- **Used by**: internal AWS services, agents running with IRSA, CLI tools using `awscurl`

The IAM principal used in live mode should be registered in the gateway's agent registry. The typical test identity is `adp-dev-agent-runner-role`. To authenticate the test runner for the IAM path:

```bash
# Option A: Use the agent runner role directly (if running on EKS with IRSA)
# The role is automatically available via the service account

# Option B: Assume the role explicitly
aws sts assume-role \
  --role-arn arn:aws:iam::879318057152:role/adp-dev-agent-runner-role \
  --role-session-name e2e-test \
  --output json

# Export the temporary credentials
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_SESSION_TOKEN=...
```

## Running Tests

### Unit mode (default -- fast, no AWS)

```bash
cd modules/gateway
uv run pytest tests/e2e/ -v
```

### Live mode (runs against the deployed gateway)

When Terraform is applied with `create_test_users = true` (default in `environments/dev/`), Cognito credentials and pool IDs live in Secrets Manager and SSM. The test runner auto-discovers them; all you need is `TEST_ENV=dev` and AWS credentials:

```bash
TEST_ENV=dev AWS_PROFILE=<your-profile> uv run pytest tests/e2e/ -v
```

Config resolution order per field: environment variable → SSM Parameter Store → Secrets Manager (`adp/<env>/gateway/test-user-credentials`). Any field can still be overridden inline — useful for CI or ad-hoc runs:

```bash
TEST_ENV=dev \
API_GATEWAY_URL=https://<id>.execute-api.us-east-1.amazonaws.com/dev \
TEST_USER_EMAIL=adp-test@example.com \
TEST_USER_PASSWORD=... \
uv run pytest tests/e2e/ -v
```

The `jwt_for_admin` fixture reads `adp/<env>/gateway/test-admin-credentials` directly, so admin-endpoint tests just need the same `TEST_ENV=dev` + AWS creds.

### Run by mode

```bash
# Only unit tests
pytest tests/e2e/ -m unit -v

# Only integration tests (ASGI in-process HTTP)
pytest tests/e2e/ -m integration -v

# Only live tests (requires TEST_ENV=dev)
TEST_ENV=dev pytest tests/e2e/ -m live_only -v

# See what would run per mode (dry-run)
pytest tests/e2e/ -m "live_only or integration" --collect-only
```

### Run by category

```bash
pytest tests/e2e/ -m auth -v
pytest tests/e2e/ -m admin -v
pytest tests/e2e/ -m proxy -v
pytest tests/e2e/ -m budget -v
pytest tests/e2e/ -m ratelimit -v
pytest tests/e2e/ -m pool -v
```

### Frontend-only (live, needs Playwright)

```bash
pip install -e ".[browser]"
playwright install chromium
uv run pytest tests/e2e/test_frontend_smoke.py -v
```

### Run with JUnit XML + report

```bash
uv run pytest tests/e2e/ -v --junitxml=/tmp/gateway-e2e.xml
python tests/scripts/report.py /tmp/gateway-e2e.xml
```

The report shows per-category counts broken down by mode (live/integration/unit) and flags categories with zero live tests.

## Pytest Markers

| Marker | Description |
|--------|-------------|
| `@pytest.mark.unit` | Runs against fixtures/mocks only |
| `@pytest.mark.integration` | Exercises ASGI app via HTTP (dual-mode) |
| `@pytest.mark.live_only` | Only makes sense in live mode; auto-skipped in unit |
| `@pytest.mark.live` | Hits a deployed environment (may also run in unit mode) |
| `@pytest.mark.browser` | Requires Playwright |
| `@pytest.mark.e2e` | End-to-end test (all tests in `tests/e2e/`) |
| `@pytest.mark.auth` | Authentication category |
| `@pytest.mark.admin` | Admin/dashboard category |
| `@pytest.mark.proxy` | Proxy/LLM request category |
| `@pytest.mark.budget` | Budget management category |
| `@pytest.mark.ratelimit` | Rate limiting category |
| `@pytest.mark.pool` | Bedrock pool management category |
| `@pytest.mark.frontend` | Frontend smoke tests |

Every test in `tests/e2e/` has exactly one of: `@pytest.mark.unit`, `@pytest.mark.integration`, or `@pytest.mark.live_only`.

## Config Discovery

Test configuration is read by `tests/e2e/config.py`:
1. Environment variables (highest priority)
2. AWS SSM Parameter Store (e.g., `/adp/dev/gateway/cloudfront-domain`)

## Bash E2E Script

The original bash E2E (`scripts/e2e_test.sh`) remains for ops-level validation:
- M2M agent flow (client_credentials -> Bedrock call)
- Human CLI flow (Cognito user auth -> Bedrock call)
- SSE streaming verification

Run it directly against the deployed gateway:
```bash
./scripts/e2e_test.sh --cloudfront-url https://d1g6cal2ts4iis.cloudfront.net
```

## Prerequisites

1. **Python 3.12+** installed
2. **uv** package manager (recommended) or pip

## Installation

```bash
pip install -e ".[dev]"
```

## Run All Tests

```bash
pytest tests/ -v
```

## Run with Coverage

```bash
pytest tests/ --cov=src --cov-report=term-missing
```

## Contributing

When adding new tests to `tests/e2e/`:

1. Apply exactly one mode marker: `@pytest.mark.unit`, `@pytest.mark.integration`, or `@pytest.mark.live_only`
2. Apply a category marker: `@pytest.mark.auth`, `@pytest.mark.proxy`, etc.
3. Use `api_client` for API tests, `iam_signed_client` for IAM-auth tests
4. Never use `cloudfront_client` for API contract tests
5. Add docstrings referencing user stories
6. Run linter: `ruff check tests/ --fix && ruff format tests/`
