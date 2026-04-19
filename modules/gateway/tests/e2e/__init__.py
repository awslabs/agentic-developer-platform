"""
End-to-end tests package for BedrockGateway.

This package contains E2E test scenarios based on the 24 user stories
from the inception documents. Tests simulate complete user workflows
from authentication through API usage.

Dual-mode support:
- Unit mode (TEST_ENV=unit, default): tests against FastAPI ASGI app with mocks
- Live mode (TEST_ENV=dev): tests against deployed gateway with real AWS services

Test modes (every test has exactly one):
- @pytest.mark.unit: Pure Python logic with db_session + mocks
- @pytest.mark.integration: ASGI app in-process HTTP via api_client
- @pytest.mark.live_only: Real HTTP against deployed gateway (skipped in unit mode)

Authentication modes (live tests cover both):
- OAuth / Cognito JWT: via jwt_for_user, jwt_for_admin, jwt_for_agent fixtures
- IAM SigV4: via iam_signed_client fixture

User Story Categories:
- Authentication Stories (US-1.x): SSO login, token exchange, service accounts
- Proxy Stories (US-4.x): OpenAI format, Anthropic format, streaming
- Budget Stories (US-2.x): Budget management, enforcement, alerts
- Rate Limit Stories (US-3.x): Rate configuration, enforcement, headers
- Pool Stories (US-5.x): Pool configuration, round-robin, failover
- Admin Stories (US-7.x, US-8.x): Dashboard, logs, metrics
- Frontend Smoke (browser): Page load, Cognito redirect, authenticated view

Key modules:
- config.py: LiveTestConfig with env-var-first, SSM-fallback discovery
- conftest.py: Dual-mode fixtures (api_client, iam_signed_client, jwt_for_user, etc.)
"""
