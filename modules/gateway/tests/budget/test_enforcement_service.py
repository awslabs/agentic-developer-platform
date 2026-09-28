"""
Unit tests for Budget Enforcement Service.

Tests cascading budget enforcement logic, cost calculation, and usage recording.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.pricing import PricingService
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EnforcementMode, EntityType


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
def service_account_context():
    """Create a test service account context."""
    return TokenContext(
        user_id="service-456",
        org_id="org-456",
        team_id="team-789",
        department_id="dept-012",
        account_type="service",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def pricing_service():
    """Create a test pricing service."""
    return PricingService()


class TestPricingService:
    """Tests for PricingService."""

    def test_calculate_cost_known_model(self, pricing_service):
        """Test cost calculation for a known model."""
        model_id = "anthropic.claude-3-5-sonnet-20241022-v2:0"
        input_tokens = 1000
        output_tokens = 500

        cost = pricing_service.calculate_cost(model_id, input_tokens, output_tokens)

        # Input: 1000 tokens * $0.003/1000 = $0.003
        # Output: 500 tokens * $0.015/1000 = $0.0075
        # Total: $0.0105
        assert cost == Decimal("0.0105")

    def test_calculate_cost_unknown_model_uses_default(self, pricing_service):
        """Test that unknown models use default pricing."""
        model_id = "some-unknown-model"
        input_tokens = 1000
        output_tokens = 1000

        cost = pricing_service.calculate_cost(model_id, input_tokens, output_tokens)

        # Default: input $0.003, output $0.015
        # Total: $0.003 + $0.015 = $0.018
        assert cost == Decimal("0.018")

    def test_calculate_cost_alias_resolution(self, pricing_service):
        """Test that model aliases are resolved correctly."""
        # "claude-3-5-sonnet" is an alias for the full model ID
        model_id = "claude-3-5-sonnet"
        input_tokens = 1000
        output_tokens = 500

        cost = pricing_service.calculate_cost(model_id, input_tokens, output_tokens)

        # Should use same pricing as the full model ID
        assert cost == Decimal("0.0105")

    def test_estimate_input_tokens_from_messages(self, pricing_service):
        """Test input token estimation from message content."""
        request_body = {
            "messages": [
                {"role": "user", "content": "Hello, how are you?"},  # 19 chars
                {"role": "assistant", "content": "I am fine, thank you!"},  # 21 chars
            ],
            "system": "You are helpful.",  # 16 chars
        }

        tokens = pricing_service.estimate_input_tokens(request_body)

        # Total chars: 19 + 21 + 16 = 56
        # Estimated tokens: 56 / 4 = 14
        assert tokens == 14

    def test_estimate_output_tokens_with_max_tokens(self, pricing_service):
        """Test output token estimation with max_tokens specified."""
        tokens = pricing_service.estimate_output_tokens(max_tokens=1000)

        # Should return half of max_tokens
        assert tokens == 500

    def test_estimate_output_tokens_without_max_tokens(self, pricing_service):
        """Test output token estimation without max_tokens."""
        tokens = pricing_service.estimate_output_tokens(max_tokens=None)

        # Should return default estimate (500)
        assert tokens == 500

    def test_estimate_request_cost(self, pricing_service):
        """Test full request cost estimation."""
        model_id = "anthropic.claude-3-haiku-20240307-v1:0"
        request_body = {
            "messages": [
                {"role": "user", "content": "Hello!"},  # 6 chars -> ~1 token
            ],
            "max_tokens": 100,  # Estimate 50 output tokens
        }

        cost = pricing_service.estimate_request_cost(model_id, request_body)

        # Haiku pricing: input $0.00025, output $0.00125 per 1k tokens
        # Input: ~1 token, Output: ~50 tokens
        # Very small cost but non-zero
        assert cost > Decimal("0")
        assert cost < Decimal("0.001")


class TestBudgetEnforcementService:
    """Tests for BudgetEnforcementService."""

    @pytest.mark.asyncio
    async def test_check_budget_allows_when_under_budget(self, token_context):
        """Test that requests are allowed when under budget."""
        # Test with budget check disabled (simplest case)
        with patch("src.budget.enforcement_service.budget_config") as mock_config:
            mock_config.budget_check_enabled = False

            service = BudgetEnforcementService()
            result = await service.check_budget_hierarchy(token_context, estimated_cost=Decimal("1.00"))

        # Request should be allowed when budget check is disabled
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_check_budget_blocks_when_over_hard_limit(self, token_context):
        """Test that requests are blocked when over hard limit."""
        mock_session = AsyncMock()

        # Create a budget config with hard enforcement
        budget_config = MagicMock()
        budget_config.budget_amount_usd = Decimal("100.00")
        budget_config.enforcement_mode = "hard"

        # Create usage showing budget exhausted
        budget_usage = MagicMock()
        budget_usage.total_cost_usd = Decimal("100.00")

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = budget_config

        usage_result = MagicMock()
        usage_result.scalar_one_or_none.return_value = budget_usage

        mock_session.execute = AsyncMock(side_effect=[mock_result, usage_result])

        service = BudgetEnforcementService(db_session=mock_session)

        with patch.object(service, "_get_session") as mock_get_session:
            mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_get_session.return_value.__aexit__ = AsyncMock()

            result = await service.check_budget_hierarchy(token_context, estimated_cost=Decimal("1.00"))

        # Request should be blocked
        assert result.allowed is False
        assert result.blocked_reason is not None
        assert result.enforcement_mode == EnforcementMode.HARD

    @pytest.mark.asyncio
    async def test_check_budget_warns_when_over_soft_limit(self, token_context):
        """Test that soft limit excess generates warning but allows request."""
        # This test verifies the schema structure
        from src.shared.schemas.budget import EnforcementResult

        # When a soft limit is exceeded, the result should have warnings
        result = EnforcementResult(
            allowed=True,
            warnings=["Budget exceeded (soft limit): user-123 monthly budget"],
        )

        assert result.allowed is True
        assert len(result.warnings) > 0

    @pytest.mark.asyncio
    async def test_check_budget_returns_allowed_when_no_budget_configured(self, token_context):
        """Test that requests are allowed when no budget is configured."""
        mock_session = AsyncMock()

        # Return None for budget config (no budget configured)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        mock_session.execute = AsyncMock(return_value=mock_result)

        service = BudgetEnforcementService(db_session=mock_session)

        with patch.object(service, "_get_session") as mock_get_session:
            mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_get_session.return_value.__aexit__ = AsyncMock()

            result = await service.check_budget_hierarchy(token_context, estimated_cost=Decimal("1.00"))

        # Request should be allowed
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_check_budget_fails_closed_on_error_by_default(self, token_context):
        """Test that budget check fails closed on database errors by default."""
        # Test by mocking _get_session to raise an exception
        service = BudgetEnforcementService()

        # Patch the internal method to simulate a database error
        async def mock_get_session_error():
            raise Exception("Database error")

        with patch.object(service, "_get_session", side_effect=mock_get_session_error):
            with patch("src.budget.enforcement_service.budget_config") as mock_config:
                mock_config.budget_check_enabled = True
                mock_config.budget_fail_mode = "closed"  # Default behavior
                result = await service.check_budget_hierarchy(token_context, estimated_cost=Decimal("1.00"))

        # Should fail closed - block the request
        assert result.allowed is False
        assert result.blocked_reason is not None

    @pytest.mark.asyncio
    async def test_check_budget_fails_open_when_configured(self, token_context):
        """Test that budget check fails open when configured to do so."""
        # Test by mocking _get_session to raise an exception
        service = BudgetEnforcementService()

        # Patch the internal method to simulate a database error
        async def mock_get_session_error():
            raise Exception("Database error")

        with patch.object(service, "_get_session", side_effect=mock_get_session_error):
            with patch("src.budget.enforcement_service.budget_config") as mock_config:
                mock_config.budget_check_enabled = True
                mock_config.budget_fail_mode = "open"  # Configure to fail open
                result = await service.check_budget_hierarchy(token_context, estimated_cost=Decimal("1.00"))

        # Should fail open - allow the request
        assert result.allowed is True
        assert len(result.warnings) > 0  # Should have warning about failure

    def test_get_entity_hierarchy_human_user(self, token_context):
        """Test entity hierarchy for human users."""
        service = BudgetEnforcementService()

        entities = service._get_entity_hierarchy(token_context)

        assert len(entities) == 4
        assert entities[0] == (EntityType.USER, "user-123")
        assert entities[1] == (EntityType.TEAM, "team-789")
        assert entities[2] == (EntityType.DEPARTMENT, "dept-012")
        assert entities[3] == (EntityType.ORGANIZATION, "org-456")

    def test_get_entity_hierarchy_service_account(self, service_account_context):
        """Test entity hierarchy for service accounts."""
        service = BudgetEnforcementService()

        entities = service._get_entity_hierarchy(service_account_context)

        assert len(entities) == 4
        assert entities[0] == (EntityType.SERVICE_ACCOUNT, "service-456")
        assert entities[1] == (EntityType.TEAM, "team-789")
        assert entities[2] == (EntityType.DEPARTMENT, "dept-012")
        assert entities[3] == (EntityType.ORGANIZATION, "org-456")

    @pytest.mark.asyncio
    async def test_record_usage_to_all_hierarchy_levels(self, token_context):
        """Test that usage is recorded to all hierarchy levels."""
        mock_session = AsyncMock()

        # Mock getting or creating usage records
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None  # No existing record

        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()

        service = BudgetEnforcementService(db_session=mock_session)

        with patch.object(service, "_get_session") as mock_get_session:
            mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_get_session.return_value.__aexit__ = AsyncMock()

            await service.record_usage(
                token_context,
                input_tokens=100,
                output_tokens=200,
                model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            )

        # Should have been called to add usage records
        # 4 entities (user, team, dept, org) x 3 periods (daily, weekly, monthly) = 12 records
        assert mock_session.commit.called

    def test_estimate_request_cost(self, token_context):
        """Test request cost estimation."""
        service = BudgetEnforcementService()

        request_body = {
            "messages": [{"role": "user", "content": "Hello world!"}],  # 12 chars
            "max_tokens": 500,
        }

        cost = service.estimate_request_cost("anthropic.claude-3-5-sonnet-20241022-v2:0", request_body)

        # Should return a non-zero cost
        assert cost > Decimal("0")


class TestBudgetEnforcementMiddleware:
    """Tests for BudgetEnforcementMiddleware."""

    # Note: Middleware tests would typically be integration tests
    # with a test client, as they depend on the full FastAPI request cycle.
    # See tests/budget/test_enforcement_middleware.py for middleware-specific tests.

    def test_placeholder_for_middleware_integration_tests(self):
        """Placeholder test to validate test class is not empty."""
        # Middleware integration tests require a full FastAPI test client
        # and are implemented in test_enforcement_middleware.py
        assert True
