"""
End-to-end tests package for BedrockGateway.

This package contains E2E test scenarios based on the 24 user stories
from the inception documents. Tests simulate complete user workflows
from authentication through API usage.

User Story Categories:
- Authentication Stories (US-1.x): SSO login, token exchange, service accounts
- Proxy Stories (US-4.x): OpenAI format, Anthropic format, streaming
- Budget Stories (US-2.x): Budget management, enforcement, alerts
- Rate Limit Stories (US-3.x): Rate configuration, enforcement, headers
- Pool Stories (US-5.x): Pool configuration, round-robin, failover
- Admin Stories (US-7.x, US-8.x): Dashboard, logs, metrics
"""
