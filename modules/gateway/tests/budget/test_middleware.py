import json
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from src.budget.middleware import BudgetEnforcementMiddleware
from src.budget.service import BudgetService
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EnforcementMode, EnforcementResult, EntityType


@pytest.fixture
def mock_budget_service():
    """Create a mock BudgetService."""
    return MagicMock(spec=BudgetService)


@pytest.fixture
def token_context():
    """Create a test TokenContext."""
    return TokenContext(
        user_id="user-123",
        org_id="org-123",
        team_id="team-123",
        department_id="dept-123",
        account_type="human",
        is_admin=False,
        expires_at=datetime.utcnow(),
    )


@pytest.fixture
def app_with_middleware(mock_budget_service):
    """Create a FastAPI app with budget middleware."""
    app = FastAPI()

    # Add the budget middleware
    app.add_middleware(BudgetEnforcementMiddleware, enforcement_service=mock_budget_service)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/proxy/chat")
    async def proxy_chat(request: Request):
        return {"message": "response", "tokens_used": 1000}

    @app.post("/budgets/test")
    async def budget_test():
        return {"message": "budget endpoint"}

    return app, mock_budget_service


@pytest.fixture
def middleware_instance(mock_budget_service):
    """Create a middleware instance for unit testing internal methods."""
    # Create a minimal app to satisfy the middleware's __init__
    app = FastAPI()
    return BudgetEnforcementMiddleware(app, enforcement_service=mock_budget_service)


class TestBudgetEnforcementMiddleware:
    """Test suite for BudgetEnforcementMiddleware."""

    @pytest.mark.asyncio
    async def test_middleware_skips_non_api_routes(self, app_with_middleware):
        """Test that middleware skips budget checks for non-API routes."""
        app, mock_service = app_with_middleware

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")

        assert response.status_code == 200
        # Budget service should not be called for health endpoint
        mock_service.check_budget_with_cost.assert_not_called()

    @pytest.mark.asyncio
    async def test_middleware_skips_budget_endpoints(self, app_with_middleware):
        """Test that middleware skips budget checks for budget management endpoints."""
        app, mock_service = app_with_middleware

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/budgets/test")

        assert response.status_code == 200
        # Budget service should not be called for budget endpoints
        mock_service.check_budget_with_cost.assert_not_called()

    @pytest.mark.asyncio
    async def test_middleware_allows_request_when_budget_check_passes(self, app_with_middleware, token_context):
        """Test that middleware allows request when budget check passes."""
        app, mock_service = app_with_middleware

        # Mock successful budget check
        mock_service.check_budget_with_cost.return_value = EnforcementResult(allowed=True, warnings=[])
        mock_service.record_usage.return_value = None

        with patch.object(BudgetEnforcementMiddleware, "_extract_user_context", return_value=token_context):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/proxy/chat", json={"message": "Hello", "model": "claude-3-5-sonnet-20241022"})

        assert response.status_code == 200
        mock_service.check_budget_with_cost.assert_called_once()
        mock_service.record_usage.assert_called_once()

    @pytest.mark.asyncio
    async def test_middleware_blocks_request_when_budget_exceeded_hard_enforcement(self, app_with_middleware, token_context):
        """Test that middleware blocks request when budget is exceeded with hard enforcement."""
        app, mock_service = app_with_middleware

        # Mock budget exceeded with hard enforcement
        mock_service.check_budget_with_cost.return_value = EnforcementResult(
            allowed=False,
            blocked_reason="Budget exceeded for user user-123",
            exceeded_entity_type=EntityType.USER,
            exceeded_entity_id="user-123",
            budget_amount_usd=Decimal("100.00"),
            current_spend_usd=Decimal("95.00"),
            enforcement_mode=EnforcementMode.HARD,
        )

        with patch.object(BudgetEnforcementMiddleware, "_extract_user_context", return_value=token_context):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/proxy/chat", json={"message": "Hello", "model": "claude-3-5-sonnet-20241022"})

        assert response.status_code == 402  # Payment Required
        data = response.json()
        assert data["error"] == "budget_exceeded"
        assert "Budget exceeded" in data["message"]
        assert data["details"]["exceeded_entity_type"] == "user"
        assert data["details"]["exceeded_entity_id"] == "user-123"

        # Record usage should not be called when request is blocked
        mock_service.record_usage.assert_not_called()

    @pytest.mark.asyncio
    async def test_middleware_allows_request_with_warnings_soft_enforcement(self, app_with_middleware, token_context):
        """Test that middleware allows request with warnings for soft enforcement."""
        app, mock_service = app_with_middleware

        # Mock budget exceeded with soft enforcement
        mock_service.check_budget_with_cost.return_value = EnforcementResult(allowed=True, warnings=["Budget exceeded for user user-123"])
        mock_service.record_usage.return_value = None

        with patch.object(BudgetEnforcementMiddleware, "_extract_user_context", return_value=token_context):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/proxy/chat", json={"message": "Hello", "model": "claude-3-5-sonnet-20241022"})

        assert response.status_code == 200
        # Check that warnings are included in response headers
        assert "X-Budget-Warnings" in response.headers
        warnings = json.loads(response.headers["X-Budget-Warnings"])
        assert len(warnings) == 1
        assert "Budget exceeded" in warnings[0]

        mock_service.record_usage.assert_called_once()

    @pytest.mark.asyncio
    async def test_middleware_handles_budget_check_failure_gracefully(self, app_with_middleware, token_context):
        """Test that middleware handles budget check failures gracefully."""
        app, mock_service = app_with_middleware

        # Mock budget service raising an exception
        mock_service.check_budget_with_cost.side_effect = Exception("Database error")
        mock_service.record_usage.return_value = None

        with (
            patch.object(BudgetEnforcementMiddleware, "_extract_user_context", return_value=token_context),
            patch("src.budget.middleware.logger") as mock_logger,
        ):  # Capture logging calls
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/proxy/chat", json={"message": "Hello", "model": "claude-3-5-sonnet-20241022"})

        # Request should still proceed when budget check fails
        assert response.status_code == 200
        mock_logger.warning.assert_called_once()
        # Verify the warning was called with expected message
        call_args = mock_logger.warning.call_args
        assert call_args[0][0] == "Budget check failed"
        assert call_args[1]["extra"]["error"] == "Database error"

    @pytest.mark.asyncio
    async def test_middleware_estimates_request_cost_from_json_body(self, middleware_instance):
        """Test that middleware estimates request cost from JSON body."""
        # Create a mock request with JSON body
        mock_request = MagicMock()
        mock_request.method = "POST"

        async def async_body():
            return json.dumps(
                {
                    "model": "claude-3-5-sonnet-20241022",
                    "prompt": "Hello world! " * 100,  # ~1200 characters
                }
            ).encode()

        mock_request.body = async_body

        # Test cost estimation
        estimated_cost = await middleware_instance._estimate_request_cost(mock_request)

        # Should return a positive cost estimate
        assert estimated_cost > Decimal("0")

    @pytest.mark.asyncio
    async def test_middleware_uses_default_cost_for_empty_request(self, middleware_instance):
        """Test that middleware uses default cost for empty request."""
        # Create a mock request with empty body
        mock_request = MagicMock()
        mock_request.method = "GET"

        async def async_body():
            return b""

        mock_request.body = async_body

        # Test cost estimation
        estimated_cost = await middleware_instance._estimate_request_cost(mock_request)

        # Should use default cost
        assert estimated_cost == Decimal("0.01")

    def test_middleware_should_check_budget_for_api_routes(self, middleware_instance):
        """Test that middleware correctly identifies routes that should be checked."""
        # Create mock requests
        api_request = MagicMock()
        api_request.url.path = "/proxy/chat"
        api_request.method = "POST"

        health_request = MagicMock()
        health_request.url.path = "/health"
        health_request.method = "GET"

        budget_request = MagicMock()
        budget_request.url.path = "/budgets/create"
        budget_request.method = "POST"

        # Test route checking
        assert middleware_instance._should_check_budget(api_request) is True
        assert middleware_instance._should_check_budget(health_request) is False
        assert middleware_instance._should_check_budget(budget_request) is False

    def test_middleware_estimates_tokens_from_content(self, middleware_instance):
        """Test token estimation from request content."""
        # Test with prompt field
        request_data = {"prompt": "Hello world! " * 100}  # ~1200 characters
        tokens = middleware_instance._estimate_tokens_from_content(request_data)
        assert tokens > 100  # Should estimate ~300 tokens

        # Test with messages field
        request_data = {
            "messages": [
                {"content": "Hello"},
                {"content": "World"},
            ]
        }
        tokens = middleware_instance._estimate_tokens_from_content(request_data)
        assert tokens >= 10  # Should have minimum tokens

        # Test with empty content
        request_data = {}
        tokens = middleware_instance._estimate_tokens_from_content(request_data)
        assert tokens == 10  # Should use minimum

    @pytest.mark.asyncio
    async def test_middleware_records_usage_with_actual_tokens(self, app_with_middleware, token_context):
        """Test that middleware records usage with actual token counts when available."""
        app, mock_service = app_with_middleware

        mock_service.check_budget_with_cost.return_value = EnforcementResult(allowed=True, warnings=[])
        mock_service.record_usage.return_value = None

        with (
            patch.object(BudgetEnforcementMiddleware, "_extract_user_context", return_value=token_context),
            patch.object(BudgetEnforcementMiddleware, "_extract_actual_usage", return_value=(1000, 500, "claude-3-5-sonnet-20241022")),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/proxy/chat", json={"message": "Hello", "model": "claude-3-5-sonnet-20241022"})

        assert response.status_code == 200
        # Verify record_usage was called with actual token counts
        mock_service.record_usage.assert_called_once_with(token_context, 1000, 500, "claude-3-5-sonnet-20241022")

    @pytest.mark.asyncio
    async def test_middleware_records_usage_with_estimated_tokens_fallback(self, app_with_middleware, token_context):
        """Test that middleware falls back to estimated usage when actual tokens unavailable."""
        app, mock_service = app_with_middleware

        mock_service.check_budget_with_cost.return_value = EnforcementResult(allowed=True, warnings=[])
        mock_service.record_usage.return_value = None

        with (
            patch.object(BudgetEnforcementMiddleware, "_extract_user_context", return_value=token_context),
            patch.object(
                BudgetEnforcementMiddleware,
                "_extract_actual_usage",
                return_value=(None, None, None),  # No actual usage available
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/proxy/chat", json={"message": "Hello", "model": "claude-3-5-sonnet-20241022"})

        assert response.status_code == 200
        # Verify record_usage was called (fallback logic invoked)
        mock_service.record_usage.assert_called_once()

    @pytest.mark.asyncio
    async def test_middleware_handles_record_usage_failure_gracefully(self, app_with_middleware, token_context):
        """Test that middleware handles record usage failures gracefully."""
        app, mock_service = app_with_middleware

        mock_service.check_budget_with_cost.return_value = EnforcementResult(allowed=True, warnings=[])
        # Mock record_usage raising an exception
        mock_service.record_usage.side_effect = Exception("Database error")

        with (
            patch.object(BudgetEnforcementMiddleware, "_extract_user_context", return_value=token_context),
            patch("src.budget.middleware.logger") as mock_logger,
        ):  # Capture logging calls
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/proxy/chat", json={"message": "Hello", "model": "claude-3-5-sonnet-20241022"})

        # Request should still succeed even if usage recording fails
        assert response.status_code == 200
        mock_logger.warning.assert_called_once()
        # Verify the warning was called with expected message
        call_args = mock_logger.warning.call_args
        assert call_args[0][0] == "Failed to record budget usage"
        assert call_args[1]["extra"]["error"] == "Database error"

    @pytest.mark.asyncio
    async def test_middleware_extract_actual_usage_from_headers(self, middleware_instance):
        """Test extracting actual usage from response headers."""
        # Create mock response with usage headers
        mock_response = MagicMock()
        mock_response.headers = {
            "X-Tokens-Input": "1000",
            "X-Tokens-Output": "500",
            "X-Model-Name": "claude-3-5-sonnet-20241022",
        }

        # Test usage extraction
        tokens_in, tokens_out, model = await middleware_instance._extract_actual_usage(MagicMock(), mock_response)

        assert tokens_in == 1000
        assert tokens_out == 500
        assert model == "claude-3-5-sonnet-20241022"

    @pytest.mark.asyncio
    async def test_middleware_extract_actual_usage_returns_none_when_missing(self, middleware_instance):
        """Test extracting actual usage returns None when headers missing."""
        # Create mock response without usage headers
        mock_response = MagicMock()
        mock_response.headers = {}

        # Test usage extraction
        tokens_in, tokens_out, model = await middleware_instance._extract_actual_usage(MagicMock(), mock_response)

        assert tokens_in is None
        assert tokens_out is None
        assert model is None
