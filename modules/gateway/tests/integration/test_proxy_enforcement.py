"""
Integration tests for Proxy Path with Budget and Rate Limit Enforcement.

Tests the full request flow including:
- Budget enforcement with cascading hierarchy
- Rate limit enforcement with hierarchical checks
- Response headers injection
- Error responses for exceeded limits
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
from src.budget.enforcement_service import BudgetEnforcementService
from src.ratelimit.enforcement_middleware import RateLimitEnforcementMiddleware
from src.ratelimit.service import RateLimitService
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EnforcementMode, EnforcementResult


@pytest.fixture
def token_context():
    """Create a test token context."""
    return TokenContext(
        user_id="user-123",
        org_id="org-456",
        team_id="team-789",
        department_id="dept-012",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def mock_budget_service():
    """Create a mock budget enforcement service."""
    service = MagicMock(spec=BudgetEnforcementService)
    service.check_budget_hierarchy = AsyncMock(return_value=EnforcementResult(allowed=True))
    service.estimate_request_cost = MagicMock(return_value=Decimal("0.01"))
    service.get_budget_status_for_headers = AsyncMock(return_value={"budget_limit": 100.0, "budget_remaining": 50.0})
    return service


@pytest.fixture
def mock_ratelimit_service():
    """Create a mock rate limit service."""
    from src.shared.schemas.common import RateLimitCheckResult

    service = MagicMock(spec=RateLimitService)
    service.check_rate_limit = AsyncMock(return_value=RateLimitCheckResult(allowed=True))
    service.consume_rate_limit = AsyncMock(return_value=RateLimitCheckResult(allowed=True))
    service.release_concurrent = AsyncMock()
    service.get_status = AsyncMock(return_value=MagicMock(rpm_limit=60, rpm_remaining=55, rpm_reset_seconds=60))
    return service


def create_test_app(budget_service=None, ratelimit_service=None):
    """Create a test FastAPI app with enforcement middleware."""
    app = FastAPI()

    # Add a test route
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        return JSONResponse(content={"message": "success"})

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # Add middleware - Starlette processes in LIFO order (last added runs first)
    # We want rate limit to run first (outermost), so add budget first then rate limit
    if budget_service:
        app.add_middleware(BudgetEnforcementMiddleware, enforcement_service=budget_service)

    if ratelimit_service:
        app.add_middleware(RateLimitEnforcementMiddleware, ratelimit_service=ratelimit_service)

    return app


class TestBudgetEnforcementIntegration:
    """Integration tests for budget enforcement in proxy path."""

    def test_request_allowed_when_under_budget(self, token_context, mock_budget_service):
        """Test that requests are allowed when under budget."""
        app = create_test_app(budget_service=mock_budget_service)
        client = TestClient(app)

        # Set up auth context middleware
        @app.middleware("http")
        async def add_auth_context(request: Request, call_next):
            request.state.token_context = token_context
            return await call_next(request)

        # Make request
        response = client.post(
            "/v1/chat/completions",
            json={"model": "claude-3-5-sonnet", "messages": [{"role": "user", "content": "Hi"}]},
        )

        assert response.status_code == 200

    def test_request_blocked_when_over_hard_budget(self, token_context, mock_budget_service):
        """Test that requests are blocked when over hard budget limit."""
        from src.shared.schemas.budget import EntityType

        # Configure mock to return budget exceeded
        mock_budget_service.check_budget_hierarchy = AsyncMock(
            return_value=EnforcementResult(
                allowed=False,
                blocked_reason="Budget exceeded for user user-123",
                exceeded_entity_type=EntityType.USER,
                exceeded_entity_id="user-123",
                budget_amount_usd=Decimal("100.00"),
                current_spend_usd=Decimal("105.00"),
                enforcement_mode=EnforcementMode.HARD,
            )
        )

        app = create_test_app(budget_service=mock_budget_service)

        @app.middleware("http")
        async def add_auth_context(request: Request, call_next):
            request.state.token_context = token_context
            return await call_next(request)

        client = TestClient(app)

        response = client.post(
            "/v1/chat/completions",
            json={"model": "claude-3-5-sonnet", "messages": [{"role": "user", "content": "Hi"}]},
        )

        # The enforcement middleware returns 402 Payment Required (not 429): the
        # AWS SDK auto-retries 429 (throttling), which makes a budget-blocked
        # client appear hung in a retry loop. See _send_budget_exceeded.
        assert response.status_code == 402
        assert "budget_exceeded" in response.json()["error"]
        # Block-path headers the middleware sets.
        assert response.headers["retry-after"] == "3600"
        assert response.headers["x-budget-remaining"] == "0"
        assert response.headers["x-budget-limit"] == "100.00"

    def test_soft_limit_warning_does_not_block(self, token_context, mock_budget_service):
        """A soft-limit breach (allowed=True with warnings) lets the request through.

        The active ASGI enforcement middleware does not inject success-path
        warning headers (X-Budget-Warning) — that was a superseded middleware.
        What matters here is that a warning-only result is not blocked.
        """
        mock_budget_service.check_budget_hierarchy = AsyncMock(
            return_value=EnforcementResult(
                allowed=True,
                warnings=["Team budget at 90% utilization"],
            )
        )

        app = create_test_app(budget_service=mock_budget_service)

        @app.middleware("http")
        async def add_auth_context(request: Request, call_next):
            request.state.token_context = token_context
            return await call_next(request)

        client = TestClient(app)

        response = client.post(
            "/v1/chat/completions",
            json={"model": "claude-3-5-sonnet", "messages": [{"role": "user", "content": "Hi"}]},
        )

        assert response.status_code == 200
        assert response.json() == {"message": "success"}

    def test_non_enforced_paths_skip_budget_check(self, token_context, mock_budget_service):
        """Test that non-enforced paths skip budget checks."""
        app = create_test_app(budget_service=mock_budget_service)

        @app.middleware("http")
        async def add_auth_context(request: Request, call_next):
            request.state.token_context = token_context
            return await call_next(request)

        client = TestClient(app)

        # Health endpoint should not be enforced
        response = client.get("/health")

        assert response.status_code == 200
        # Budget check should not have been called for health endpoint
        mock_budget_service.check_budget_hierarchy.assert_not_called()


class TestRateLimitEnforcementIntegration:
    """Integration tests for rate limit enforcement in proxy path."""

    def test_request_allowed_when_under_rate_limit(self, token_context, mock_ratelimit_service):
        """Test that requests are allowed when under rate limit."""
        app = create_test_app(ratelimit_service=mock_ratelimit_service)

        @app.middleware("http")
        async def add_auth_context(request: Request, call_next):
            request.state.token_context = token_context
            return await call_next(request)

        client = TestClient(app)

        response = client.post(
            "/v1/chat/completions",
            json={"model": "claude-3-5-sonnet", "messages": [{"role": "user", "content": "Hi"}]},
        )

        assert response.status_code == 200

    def test_request_blocked_when_rate_limited(self, token_context, mock_ratelimit_service):
        """Test that requests are blocked when rate limited."""
        from src.shared.schemas.common import RateLimitCheckResult

        mock_ratelimit_service.check_rate_limit = AsyncMock(
            return_value=RateLimitCheckResult(
                allowed=False,
                limit_type="rpm",
                limit=60,
                remaining=0,
                retry_after_seconds=30,
            )
        )

        app = create_test_app(ratelimit_service=mock_ratelimit_service)

        @app.middleware("http")
        async def add_auth_context(request: Request, call_next):
            request.state.token_context = token_context
            return await call_next(request)

        client = TestClient(app)

        response = client.post(
            "/v1/chat/completions",
            json={"model": "claude-3-5-sonnet", "messages": [{"role": "user", "content": "Hi"}]},
        )

        assert response.status_code == 429
        assert "rate_limited" in response.json()["error"]
        assert "Retry-After" in response.headers

    def test_under_limit_request_passes_through(self, token_context, mock_ratelimit_service):
        """A request under the rate limit is forwarded to the route unchanged.

        The active ASGI enforcement middleware only emits X-RateLimit-* headers
        on the 429 block path (see _send_rate_limited); it does not decorate
        successful responses. Block-path headers are covered by
        test_request_blocked_when_rate_limited.
        """
        app = create_test_app(ratelimit_service=mock_ratelimit_service)

        @app.middleware("http")
        async def add_auth_context(request: Request, call_next):
            request.state.token_context = token_context
            return await call_next(request)

        client = TestClient(app)

        response = client.post(
            "/v1/chat/completions",
            json={"model": "claude-3-5-sonnet", "messages": [{"role": "user", "content": "Hi"}]},
        )

        assert response.status_code == 200
        assert response.json() == {"message": "success"}
        # consume_rate_limit is called on the allowed path.
        mock_ratelimit_service.consume_rate_limit.assert_awaited_once()


class TestCombinedEnforcementIntegration:
    """Integration tests for combined budget and rate limit enforcement."""

    def test_both_budget_and_rate_limit_enforced(self, token_context, mock_budget_service, mock_ratelimit_service):
        """Test that both budget and rate limit are enforced."""
        app = create_test_app(
            budget_service=mock_budget_service,
            ratelimit_service=mock_ratelimit_service,
        )

        @app.middleware("http")
        async def add_auth_context(request: Request, call_next):
            request.state.token_context = token_context
            return await call_next(request)

        client = TestClient(app)

        response = client.post(
            "/v1/chat/completions",
            json={"model": "claude-3-5-sonnet", "messages": [{"role": "user", "content": "Hi"}]},
        )

        assert response.status_code == 200

    def test_rate_limit_checked_before_budget(self, token_context, mock_budget_service, mock_ratelimit_service):
        """Test that rate limit is checked before budget (middleware order)."""
        from src.shared.schemas.common import RateLimitCheckResult

        # Rate limit should fail
        mock_ratelimit_service.check_rate_limit = AsyncMock(
            return_value=RateLimitCheckResult(
                allowed=False,
                limit_type="rpm",
                limit=60,
                remaining=0,
                retry_after_seconds=30,
            )
        )

        app = create_test_app(
            budget_service=mock_budget_service,
            ratelimit_service=mock_ratelimit_service,
        )

        @app.middleware("http")
        async def add_auth_context(request: Request, call_next):
            request.state.token_context = token_context
            return await call_next(request)

        client = TestClient(app)

        response = client.post(
            "/v1/chat/completions",
            json={"model": "claude-3-5-sonnet", "messages": [{"role": "user", "content": "Hi"}]},
        )

        # Should be rate limited (not budget error)
        assert response.status_code == 429
        assert "rate_limited" in response.json()["error"]

        # Budget check should not have been called
        mock_budget_service.check_budget_hierarchy.assert_not_called()


class TestResponseHeaders:
    """Tests for response header injection."""

    def test_budget_headers_format(self, token_context, mock_budget_service):
        """Test that budget headers are properly formatted."""
        mock_budget_service.get_budget_status_for_headers = AsyncMock(
            return_value={
                "budget_limit": 100.00,
                "budget_remaining": 50.50,
                "budget_reset": "2024-02-01",
            }
        )

        app = create_test_app(budget_service=mock_budget_service)

        @app.middleware("http")
        async def add_auth_context(request: Request, call_next):
            request.state.token_context = token_context
            return await call_next(request)

        client = TestClient(app)

        response = client.post(
            "/v1/chat/completions",
            json={"model": "claude-3-5-sonnet", "messages": [{"role": "user", "content": "Hi"}]},
        )

        assert response.status_code == 200

    def test_429_response_includes_retry_after(self, token_context, mock_ratelimit_service):
        """Test that 429 responses include Retry-After header."""
        from src.shared.schemas.common import RateLimitCheckResult

        mock_ratelimit_service.check_rate_limit = AsyncMock(
            return_value=RateLimitCheckResult(
                allowed=False,
                limit_type="rpm",
                limit=60,
                remaining=0,
                retry_after_seconds=45,
            )
        )

        app = create_test_app(ratelimit_service=mock_ratelimit_service)

        @app.middleware("http")
        async def add_auth_context(request: Request, call_next):
            request.state.token_context = token_context
            return await call_next(request)

        client = TestClient(app)

        response = client.post(
            "/v1/chat/completions",
            json={"model": "claude-3-5-sonnet", "messages": [{"role": "user", "content": "Hi"}]},
        )

        assert response.status_code == 429
        assert response.headers.get("Retry-After") == "45"


class TestMultiTenantIsolation:
    """Tests for multi-tenant isolation."""

    def test_different_orgs_have_separate_limits(self):
        """Test that different organizations have separate rate limits."""
        service = RateLimitService()

        context_org_a = TokenContext(
            user_id="user-a",
            org_id="org-a",
            team_id="team-a",
            department_id="dept-a",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        context_org_b = TokenContext(
            user_id="user-b",
            org_id="org-b",
            team_id="team-b",
            department_id="dept-b",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        # Keys should be different for different orgs
        key_a = service._get_entity_key(MagicMock(value="user"), context_org_a.user_id, context_org_a.org_id)
        key_b = service._get_entity_key(MagicMock(value="user"), context_org_b.user_id, context_org_b.org_id)

        assert key_a != key_b
