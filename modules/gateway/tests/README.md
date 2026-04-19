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
│   ├── test_auth_proxy_flow.py      # Auth → Token → Proxy flow
│   ├── test_budget_enforcement.py   # Budget check → Enforcement → Usage
│   ├── test_ratelimit_headers.py    # Rate limit → Enforcement → Headers
│   ├── test_pool_failover.py        # Pool round-robin → Failover
│   └── test_middleware_chain.py     # Middleware execution order
├── e2e/                     # End-to-end tests (user story scenarios)
│   ├── __init__.py
│   ├── test_authentication_stories.py  # US-1.4, US-1.5, US-1.6
│   ├── test_proxy_stories.py           # US-4.1, US-4.2, US-4.3
│   ├── test_budget_stories.py          # US-2.1, US-2.2, US-2.3, US-2.4
│   ├── test_ratelimit_stories.py       # US-3.1, US-3.2, US-3.3
│   ├── test_pool_stories.py            # US-1.2, US-5.1
│   └── test_admin_stories.py           # US-7.x, US-8.x
├── auth/                    # Unit tests for auth module
├── proxy/                   # Unit tests for proxy module
├── budget/                  # Unit tests for budget module
├── ratelimit/               # Unit tests for rate limit module
├── pool/                    # Unit tests for pool module
├── admin/                   # Unit tests for admin module
├── usage/                   # Unit tests for usage module
└── cli/                     # Unit tests for CLI tools
```

## Prerequisites

1. **Python 3.12+** installed
2. **pip** package manager
3. **Docker** (optional, for running tests with real services)

## Installation

Install development dependencies:

```bash
pip install -e ".[dev]"
```

This installs:
- pytest==8.3.4
- pytest-asyncio==0.25.0
- pytest-cov==6.0.0
- fakeredis==2.26.2
- moto==5.0.27
- ruff==0.9.6

## Running Tests

### Run All Tests

```bash
pytest tests/ -v
```

### Run Unit Tests Only

```bash
# Run tests for a specific module
pytest tests/auth/ -v
pytest tests/budget/ -v
pytest tests/proxy/ -v
```

### Run Integration Tests

```bash
pytest tests/integration/ -v
```

### Run E2E Tests

```bash
pytest tests/e2e/ -v
```

### Run Tests by Marker

```bash
# Run only integration tests
pytest -m integration -v

# Run only E2E tests
pytest -m e2e -v

# Run slow tests
pytest -m slow -v
```

### Run with Coverage

```bash
# Full coverage report
pytest tests/ --cov=src --cov-report=term-missing

# Coverage for specific module
pytest tests/auth/ --cov=src/auth --cov-report=term-missing
pytest tests/budget/ --cov=src/budget --cov-report=term-missing
```

### Run with HTML Coverage Report

```bash
pytest tests/ --cov=src --cov-report=html
# Open htmlcov/index.html in browser
```

## Using Docker Compose Test Environment

The `docker-compose.test.yml` file provides a complete test environment with:
- PostgreSQL database
- Redis for rate limiting
- LocalStack for AWS service mocking
- BedrockGateway application

### Start Test Environment

```bash
# Start all services
docker-compose -f docker-compose.test.yml up -d

# Wait for services to be healthy
docker-compose -f docker-compose.test.yml ps
```

### Run Tests in Docker

```bash
# Run test runner service
docker-compose -f docker-compose.test.yml run test-runner
```

### View Test Results

```bash
# Results are stored in test-results volume
docker-compose -f docker-compose.test.yml exec test-runner cat /app/test-results/results.xml
```

### Stop Test Environment

```bash
docker-compose -f docker-compose.test.yml down -v
```

## Test Fixtures

### Factory Functions

Create test entities easily with factory functions:

```python
from tests.fixtures.factories import (
    create_org,
    create_department,
    create_team,
    create_user,
    create_service_account,
    create_token,
    create_budget_config,
    create_rate_limit_config,
    create_pool_account,
)

# Example usage in tests
async def test_something(db_session):
    org = await create_org(db_session, name="Test Org")
    dept = await create_department(db_session, org.id, name="Engineering")
    team = await create_team(db_session, org.id, dept.id, name="Backend")
    user = await create_user(db_session, org.id, team.id, email="test@example.com")
```

### Mock AWS Clients

Use mock AWS clients for testing without real AWS calls:

```python
from tests.fixtures.mock_aws import (
    MockSTSClient,
    MockBedrockClient,
)

# Mock STS for authentication tests
mock_sts = MockSTSClient(
    account_id="123456789012",
    role_arn="arn:aws:sts::123456789012:assumed-role/TestRole/session",
)

# Mock Bedrock for proxy tests
mock_bedrock = MockBedrockClient(
    response_text="Mock response",
    input_tokens=100,
    output_tokens=200,
)
```

### Seed Data

Populate test database with realistic data:

```python
from tests.fixtures.seed_data import (
    seed_test_database,
    seed_budget_data,
    seed_rate_limit_data,
    clear_test_data,
)

async def test_with_seeded_data(db_session):
    # Seed complete test environment
    data = await seed_test_database(db_session)

    # Access seeded entities
    org = data["org1"]
    user_alice = data["user_alice"]
    raw_token = data["raw_token_alice"]
```

## Writing Tests

### Test File Naming

- Unit tests: `test_{module}_*.py` (e.g., `test_auth_service.py`)
- Integration tests: `test_{flow}_*.py` (e.g., `test_auth_proxy_flow.py`)
- E2E tests: `test_{feature}_stories.py` (e.g., `test_authentication_stories.py`)

### Test Function Naming

```python
# Clear, descriptive names
async def test_valid_token_allows_proxy_request():
    ...

async def test_expired_token_returns_401():
    ...

async def test_budget_exceeded_returns_429_with_details():
    ...
```

### Using Markers

```python
import pytest

@pytest.mark.integration
class TestAuthProxyFlow:
    ...

@pytest.mark.e2e
class TestHumanUserAuthentication:
    ...

@pytest.mark.slow
async def test_large_dataset_processing():
    ...
```

### Async Tests

All tests are async by default (configured in `pyproject.toml`):

```python
@pytest.mark.asyncio
async def test_async_operation(db_session):
    result = await some_async_function()
    assert result is not None
```

## Troubleshooting

### Common Issues

1. **Database connection errors**
   ```bash
   # Ensure test database exists
   # For SQLite (default in tests), no setup needed

   # For PostgreSQL, create test database:
   createdb bedrockgw_test
   ```

2. **Import errors**
   ```bash
   # Install package in development mode
   pip install -e ".[dev]"
   ```

3. **Async test failures**
   ```bash
   # Ensure pytest-asyncio is installed
   pip install pytest-asyncio
   ```

4. **Redis connection errors**
   ```bash
   # Tests use fakeredis by default
   # Or set REDIS_URL="" to disable Redis
   ```

### Debug Mode

```bash
# Run single test with verbose output
pytest tests/integration/test_auth_proxy_flow.py::TestAuthProxyFlow::test_valid_token_allows_proxy_request -v -s

# Show locals on failure
pytest tests/ --tb=long --showlocals
```

### Collect Tests Without Running

```bash
# Verify all tests are discoverable
pytest tests/integration/ tests/e2e/ --collect-only
```

## Test Coverage Goals

- **Unit tests**: 80%+ line coverage per module
- **Integration tests**: Cover all cross-unit interactions
- **E2E tests**: Cover all user story acceptance criteria

## CI/CD Integration

Tests are automatically run in CI/CD pipelines:

```yaml
# Example GitHub Actions step
- name: Run tests
  run: |
    pip install -e ".[dev]"
    pytest tests/ -v --cov=src --cov-report=xml
```

## E2E Tests (Dual-Mode: Unit + Live)

The `tests/e2e/` directory contains end-to-end tests that run in two modes:

### Unit mode (default — fast, no AWS)

Tests run against the FastAPI ASGI app with mocked services:

```bash
cd modules/gateway
uv run pytest tests/e2e/ -v
```

### Live mode (runs against the deployed gateway)

Tests hit the actual deployed gateway and Cognito:

```bash
TEST_ENV=dev \
API_GATEWAY_URL=https://59o2rakc50.execute-api.us-east-1.amazonaws.com/dev \
CLOUDFRONT_DOMAIN=d1g6cal2ts4iis.cloudfront.net \
COGNITO_USER_POOL_ID=us-east-1_JEhv9xSGG \
COGNITO_CLIENT_ID=6cg7ba3hb4v41vbhm0cg8pl17j \
COGNITO_AGENT_CLIENT_ID=378cm2jdj3rjt2os4cthub7267 \
TEST_USER_EMAIL=adp-test@example.com \
TEST_USER_PASSWORD=... \
uv run pytest tests/e2e/ -v -m "live or not live_only"
```

**Important — which endpoint gets hit:**

- `api_client` fixture → **REST API Gateway** (`API_GATEWAY_URL`). Use for every API contract test (auth, proxy, admin, budget, ratelimit, pool). Bypasses CloudFront so API responses aren't masked by the SPA's `index.html` fallback.
- `cloudfront_client` fixture → **CloudFront** (`CLOUDFRONT_DOMAIN`). Use only for frontend smoke tests and deliberate CDN-layer checks (response-header policies, `/api/*` routing rule).

Do not hit CloudFront for API contract tests. CloudFront routes `/*` to the S3 origin serving the SPA; any unknown URL under `/` returns `200 index.html`, which masks real backend 4xx/5xx responses.

### Frontend-only (live, needs Playwright)

```bash
pip install -e ".[browser]"
playwright install chromium
uv run pytest tests/e2e/test_frontend_smoke.py -v
```

### Pytest Markers

| Marker | Description |
|--------|-------------|
| `@pytest.mark.unit` | Runs against fixtures/mocks (default) |
| `@pytest.mark.live` | Hits a deployed environment, requires env vars |
| `@pytest.mark.live_only` | Only makes sense in live mode; skipped in unit |
| `@pytest.mark.browser` | Requires Playwright |
| `@pytest.mark.e2e` | End-to-end test |

Default `pytest tests/e2e/` = unit only (live_only tests are skipped).
Live runs: explicit `-m "live or not live_only"`.

### Config Discovery

Test configuration is read by `tests/e2e/config.py`:
1. Environment variables (highest priority)
2. AWS SSM Parameter Store (e.g., `/adp/dev/gateway/cloudfront-domain`)

### Bash E2E Script

The original bash E2E (`scripts/e2e_test.sh`) remains for ops-level validation:
- M2M agent flow (client_credentials → Bedrock call)
- Human CLI flow (Cognito user auth → Bedrock call)
- SSE streaming verification

Run it directly against the deployed gateway:
```bash
./scripts/e2e_test.sh --cloudfront-url https://d1g6cal2ts4iis.cloudfront.net
```

---

## Contributing

When adding new tests:

1. Follow existing naming conventions
2. Use appropriate markers (`@pytest.mark.integration`, `@pytest.mark.e2e`)
3. Add docstrings referencing user stories
4. Use factory functions for test data
5. Clean up test data after tests (use fixtures)
6. Run linter before committing: `ruff check tests/ --fix && ruff format tests/`
