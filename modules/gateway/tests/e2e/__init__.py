"""
End-to-end tests package for BedrockGateway.

This package contains E2E test scenarios based on the 24 user stories
from the inception documents. Tests simulate complete user workflows
from authentication through API usage.

Dual-mode support:
- Unit mode (TEST_ENV=unit, default): tests against FastAPI ASGI app with mocks
- Live mode (TEST_ENV=dev): tests against deployed gateway with real AWS services

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
- conftest.py: Dual-mode fixtures (api_client, jwt_for_user, jwt_for_agent, etc.)
"""
