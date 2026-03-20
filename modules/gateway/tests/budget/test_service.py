from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.budget.service import BudgetService
from src.budget.utils import get_period_start_end
from src.shared.exceptions import ValidationError
from src.shared.models.base import Base
from src.shared.models.budget import BudgetUsage
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import (
    BudgetCreateRequest,
    BudgetUpdateRequest,
    CostCalculationRequest,
    CostRecordRequest,
    EnforcementMode,
    EntityType,
    PeriodType,
)


@pytest.fixture
async def async_session():
    """Create an in-memory SQLite database for testing."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session_factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def budget_service(async_session):
    """Create a BudgetService instance with test database session."""
    return BudgetService(async_session)


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
def sample_budget_request():
    """Create a sample budget creation request."""
    return BudgetCreateRequest(
        entity_type=EntityType.USER,
        entity_id="user-123",
        period_type=PeriodType.MONTHLY,
        budget_amount_usd=Decimal("100.00"),
        enforcement_mode=EnforcementMode.HARD,
    )


class TestBudgetService:
    """Test suite for BudgetService."""

    # Budget Management Tests

    @pytest.mark.asyncio
    async def test_create_budget_success(self, budget_service, sample_budget_request):
        """Test successful budget creation."""
        budget = await budget_service.create_budget(sample_budget_request, "org-123")

        assert budget.entity_type == EntityType.USER
        assert budget.entity_id == "user-123"
        assert budget.period_type == PeriodType.MONTHLY
        assert budget.budget_amount_usd == Decimal("100.00")
        assert budget.enforcement_mode == EnforcementMode.HARD
        assert budget.org_id == "org-123"

    @pytest.mark.asyncio
    async def test_create_budget_duplicate(self, budget_service, sample_budget_request):
        """Test creating duplicate budget raises error."""
        # Create first budget
        await budget_service.create_budget(sample_budget_request, "org-123")

        # Try to create duplicate
        with pytest.raises(ValidationError, match="Budget already exists"):
            await budget_service.create_budget(sample_budget_request, "org-123")

    @pytest.mark.asyncio
    async def test_create_budget_invalid_amount(self, budget_service):
        """Test creating budget with invalid amount raises error."""
        import pydantic

        # Test that Pydantic schema validation rejects negative amounts
        with pytest.raises(pydantic.ValidationError):
            BudgetCreateRequest(
                entity_type=EntityType.USER,
                entity_id="user-123",
                period_type=PeriodType.MONTHLY,
                budget_amount_usd=Decimal("-10.00"),  # Invalid negative amount
                enforcement_mode=EnforcementMode.HARD,
            )

    @pytest.mark.asyncio
    async def test_get_budget_success(self, budget_service, sample_budget_request):
        """Test successful budget retrieval."""
        # Create budget
        created_budget = await budget_service.create_budget(sample_budget_request, "org-123")

        # Retrieve budget
        retrieved_budget = await budget_service.get_budget(created_budget.id, "org-123")

        assert retrieved_budget is not None
        assert retrieved_budget.id == created_budget.id
        assert retrieved_budget.entity_type == EntityType.USER

    @pytest.mark.asyncio
    async def test_get_budget_not_found(self, budget_service):
        """Test retrieving non-existent budget returns None."""
        budget = await budget_service.get_budget("non-existent", "org-123")
        assert budget is None

    @pytest.mark.asyncio
    async def test_get_budgets_for_entity(self, budget_service):
        """Test retrieving all budgets for an entity."""
        # Create multiple budgets for same entity
        requests = [
            BudgetCreateRequest(
                entity_type=EntityType.USER,
                entity_id="user-123",
                period_type=PeriodType.DAILY,
                budget_amount_usd=Decimal("10.00"),
                enforcement_mode=EnforcementMode.SOFT,
            ),
            BudgetCreateRequest(
                entity_type=EntityType.USER,
                entity_id="user-123",
                period_type=PeriodType.MONTHLY,
                budget_amount_usd=Decimal("100.00"),
                enforcement_mode=EnforcementMode.HARD,
            ),
        ]

        for request in requests:
            await budget_service.create_budget(request, "org-123")

        # Retrieve all budgets for entity
        budgets = await budget_service.get_budgets_for_entity(EntityType.USER, "user-123", "org-123")

        assert len(budgets) == 2
        period_types = {budget.period_type for budget in budgets}
        assert period_types == {PeriodType.DAILY, PeriodType.MONTHLY}

    @pytest.mark.asyncio
    async def test_update_budget_success(self, budget_service, sample_budget_request):
        """Test successful budget update."""
        # Create budget
        budget = await budget_service.create_budget(sample_budget_request, "org-123")

        # Update budget
        update_request = BudgetUpdateRequest(
            budget_amount_usd=Decimal("200.00"),
            enforcement_mode=EnforcementMode.SOFT,
        )
        updated_budget = await budget_service.update_budget(budget.id, update_request, "org-123")

        assert updated_budget is not None
        assert updated_budget.budget_amount_usd == Decimal("200.00")
        assert updated_budget.enforcement_mode == EnforcementMode.SOFT

    @pytest.mark.asyncio
    async def test_delete_budget_success(self, budget_service, sample_budget_request):
        """Test successful budget deletion."""
        # Create budget
        budget = await budget_service.create_budget(sample_budget_request, "org-123")

        # Delete budget
        success = await budget_service.delete_budget(budget.id, "org-123")
        assert success is True

        # Verify deletion
        retrieved_budget = await budget_service.get_budget(budget.id, "org-123")
        assert retrieved_budget is None

    # Cost Calculation Tests

    @pytest.mark.asyncio
    async def test_calculate_cost(self, budget_service):
        """Test cost calculation for model usage."""
        request = CostCalculationRequest(
            model_name="claude-3-5-sonnet-20241022",
            tokens_in=1000,
            tokens_out=500,
        )

        response = await budget_service.calculate_cost(request)

        assert response.model_name == "claude-3-5-sonnet-20241022"
        assert response.tokens_in == 1000
        assert response.tokens_out == 500
        assert response.cost_usd > 0
        assert response.input_cost_per_1k_tokens == Decimal("0.003")
        assert response.output_cost_per_1k_tokens == Decimal("0.015")

    @pytest.mark.asyncio
    async def test_calculate_cost_unknown_model(self, budget_service):
        """Test cost calculation for unknown model uses default pricing."""
        request = CostCalculationRequest(
            model_name="unknown-model",
            tokens_in=1000,
            tokens_out=500,
        )

        response = await budget_service.calculate_cost(request)

        assert response.model_name == "unknown-model"
        assert response.input_cost_per_1k_tokens == Decimal("0.003")  # default pricing
        assert response.output_cost_per_1k_tokens == Decimal("0.015")  # default pricing

    # Budget Status and Usage Tests

    @pytest.mark.asyncio
    async def test_get_budget_status_with_usage(self, budget_service, async_session):
        """Test getting budget status with existing usage."""
        # Create budget
        budget_request = BudgetCreateRequest(
            entity_type=EntityType.USER,
            entity_id="user-123",
            period_type=PeriodType.MONTHLY,
            budget_amount_usd=Decimal("100.00"),
            enforcement_mode=EnforcementMode.HARD,
        )
        await budget_service.create_budget(budget_request, "org-123")

        # Create usage record
        period_start, _ = get_period_start_end(PeriodType.MONTHLY)
        usage = BudgetUsage(
            org_id="org-123",
            entity_type="user",
            entity_id="user-123",
            period_start=period_start,
            period_type="monthly",
            total_cost_usd=Decimal("75.00"),
            total_tokens=5000,
            request_count=10,
        )
        async_session.add(usage)
        await async_session.commit()

        # Get budget status
        status = await budget_service.get_budget_status(EntityType.USER, "user-123", PeriodType.MONTHLY, "org-123")

        assert status is not None
        assert status.budget_amount_usd == Decimal("100.00")
        assert status.current_spend_usd == Decimal("75.00")
        assert status.remaining_budget_usd == Decimal("25.00")
        assert status.budget_utilization_percent == 75.0
        assert status.budget_exceeded is False
        assert len(status.warnings) == 0  # Below warning threshold

    @pytest.mark.asyncio
    async def test_record_cost(self, budget_service):
        """Test recording cost against budget."""
        cost_request = CostRecordRequest(
            entity_type=EntityType.USER,
            entity_id="user-123",
            model_name="claude-3-5-sonnet-20241022",
            tokens_in=1000,
            tokens_out=500,
            request_cost_usd=Decimal("10.50"),
        )

        await budget_service.record_cost(cost_request, "org-123")

        # Verify usage was recorded for all period types
        for period_type in [PeriodType.DAILY, PeriodType.WEEKLY, PeriodType.MONTHLY]:
            usage = await budget_service.get_budget_usage(EntityType.USER, "user-123", period_type, "org-123")

            assert usage is not None
            assert usage.total_cost_usd == Decimal("10.50")
            assert usage.total_tokens == 1500
            assert usage.request_count == 1

    # Hierarchical Budget Enforcement Tests

    @pytest.mark.asyncio
    async def test_check_hierarchical_budget_allowed(self, budget_service, token_context):
        """Test hierarchical budget check when request is allowed."""
        # Create user budget
        budget_request = BudgetCreateRequest(
            entity_type=EntityType.USER,
            entity_id="user-123",
            period_type=PeriodType.MONTHLY,
            budget_amount_usd=Decimal("100.00"),
            enforcement_mode=EnforcementMode.HARD,
        )
        await budget_service.create_budget(budget_request, "org-123")

        # Check budget for small cost
        result = await budget_service.check_hierarchical_budget(token_context, Decimal("5.00"))

        assert result.allowed is True
        assert len(result.warnings) == 0

    @pytest.mark.asyncio
    async def test_check_hierarchical_budget_exceeded_hard_enforcement(self, budget_service, token_context, async_session):
        """Test hierarchical budget check with hard enforcement blocks request."""
        # Create user budget
        budget_request = BudgetCreateRequest(
            entity_type=EntityType.USER,
            entity_id="user-123",
            period_type=PeriodType.MONTHLY,
            budget_amount_usd=Decimal("100.00"),
            enforcement_mode=EnforcementMode.HARD,
        )
        await budget_service.create_budget(budget_request, "org-123")

        # Create usage that would exceed budget
        period_start, _ = get_period_start_end(PeriodType.MONTHLY)
        usage = BudgetUsage(
            org_id="org-123",
            entity_type="user",
            entity_id="user-123",
            period_start=period_start,
            period_type="monthly",
            total_cost_usd=Decimal("95.00"),
            total_tokens=5000,
            request_count=10,
        )
        async_session.add(usage)
        await async_session.commit()

        # Check budget for cost that would exceed
        result = await budget_service.check_hierarchical_budget(token_context, Decimal("10.00"))

        assert result.allowed is False
        assert result.blocked_reason is not None
        assert result.exceeded_entity_type == EntityType.USER
        assert result.exceeded_entity_id == "user-123"

    @pytest.mark.asyncio
    async def test_check_hierarchical_budget_exceeded_soft_enforcement(self, budget_service, token_context, async_session):
        """Test hierarchical budget check with soft enforcement allows request with warnings."""
        # Create user budget with soft enforcement
        budget_request = BudgetCreateRequest(
            entity_type=EntityType.USER,
            entity_id="user-123",
            period_type=PeriodType.MONTHLY,
            budget_amount_usd=Decimal("100.00"),
            enforcement_mode=EnforcementMode.SOFT,
        )
        await budget_service.create_budget(budget_request, "org-123")

        # Create usage that would exceed budget
        period_start, _ = get_period_start_end(PeriodType.MONTHLY)
        usage = BudgetUsage(
            org_id="org-123",
            entity_type="user",
            entity_id="user-123",
            period_start=period_start,
            period_type="monthly",
            total_cost_usd=Decimal("95.00"),
            total_tokens=5000,
            request_count=10,
        )
        async_session.add(usage)
        await async_session.commit()

        # Check budget for cost that would exceed
        result = await budget_service.check_hierarchical_budget(token_context, Decimal("10.00"))

        assert result.allowed is True
        assert len(result.warnings) > 0
        assert "Budget exceeded" in result.warnings[0]

    @pytest.mark.asyncio
    async def test_record_usage(self, budget_service, token_context):
        """Test recording usage across hierarchy."""
        await budget_service.record_usage(token_context, tokens_in=1000, tokens_out=500, model="claude-3-5-sonnet-20241022")

        # Verify usage was recorded for user
        user_usage = await budget_service.get_budget_usage(EntityType.USER, "user-123", PeriodType.MONTHLY, "org-123")
        assert user_usage is not None
        assert user_usage.total_tokens == 1500

        # Verify usage was recorded for team
        team_usage = await budget_service.get_budget_usage(EntityType.TEAM, "team-123", PeriodType.MONTHLY, "org-123")
        assert team_usage is not None
        assert team_usage.total_tokens == 1500

        # Verify usage was recorded for department
        dept_usage = await budget_service.get_budget_usage(EntityType.DEPARTMENT, "dept-123", PeriodType.MONTHLY, "org-123")
        assert dept_usage is not None
        assert dept_usage.total_tokens == 1500

        # Verify usage was recorded for organization
        org_usage = await budget_service.get_budget_usage(EntityType.ORGANIZATION, "org-123", PeriodType.MONTHLY, "org-123")
        assert org_usage is not None
        assert org_usage.total_tokens == 1500

    # Admin and Reporting Tests

    @pytest.mark.asyncio
    async def test_get_budget_summary(self, budget_service):
        """Test getting comprehensive budget summary."""
        # Create budget
        budget_request = BudgetCreateRequest(
            entity_type=EntityType.USER,
            entity_id="user-123",
            period_type=PeriodType.MONTHLY,
            budget_amount_usd=Decimal("100.00"),
            enforcement_mode=EnforcementMode.HARD,
        )
        await budget_service.create_budget(budget_request, "org-123")

        summary = await budget_service.get_budget_summary("user", "user-123", "org-123")

        assert summary["entity_type"] == "user"
        assert summary["entity_id"] == "user-123"
        assert len(summary["budgets"]) == 1
        assert summary["budgets"][0]["budget_amount_usd"] == 100.0
        assert summary["budgets"][0]["period_type"] == "monthly"

    @pytest.mark.asyncio
    async def test_get_organization_budget_overview(self, budget_service):
        """Test getting organization-wide budget overview."""
        # Create various budgets
        budgets = [
            BudgetCreateRequest(
                entity_type=EntityType.ORGANIZATION,
                entity_id="org-123",
                period_type=PeriodType.MONTHLY,
                budget_amount_usd=Decimal("1000.00"),
                enforcement_mode=EnforcementMode.HARD,
            ),
            BudgetCreateRequest(
                entity_type=EntityType.DEPARTMENT,
                entity_id="dept-123",
                period_type=PeriodType.MONTHLY,
                budget_amount_usd=Decimal("500.00"),
                enforcement_mode=EnforcementMode.HARD,
            ),
        ]

        for budget in budgets:
            await budget_service.create_budget(budget, "org-123")

        overview = await budget_service.get_organization_budget_overview("org-123")

        assert overview["org_id"] == "org-123"
        assert overview["total_budgets"] == 2
        assert len(overview["entities"][EntityType.ORGANIZATION.value]) == 1
        assert len(overview["entities"][EntityType.DEPARTMENT.value]) == 1

    @pytest.mark.asyncio
    async def test_get_budget_alerts(self, budget_service, async_session):
        """Test getting budget alerts for entities approaching limits."""
        # Create budget
        budget_request = BudgetCreateRequest(
            entity_type=EntityType.USER,
            entity_id="user-123",
            period_type=PeriodType.MONTHLY,
            budget_amount_usd=Decimal("100.00"),
            enforcement_mode=EnforcementMode.HARD,
        )
        await budget_service.create_budget(budget_request, "org-123")

        # Create usage that exceeds alert threshold (85% of $100 = $85)
        period_start, _ = get_period_start_end(PeriodType.MONTHLY)
        usage = BudgetUsage(
            org_id="org-123",
            entity_type="user",
            entity_id="user-123",
            period_start=period_start,
            period_type="monthly",
            total_cost_usd=Decimal("90.00"),  # 90% utilization
            total_tokens=5000,
            request_count=10,
        )
        async_session.add(usage)
        await async_session.commit()

        alerts = await budget_service.get_budget_alerts("org-123", threshold_percent=80.0)

        assert len(alerts) == 1
        alert = alerts[0]
        assert alert["entity_type"] == "user"
        assert alert["entity_id"] == "user-123"
        assert alert["utilization_percent"] == 90.0
        assert alert["alert_level"] == "warning"  # Not critical yet (< 100%)

    # Validation Tests

    @pytest.mark.asyncio
    async def test_validate_budget_hierarchy_success(self, budget_service, async_session):
        """Test successful budget hierarchy validation."""
        # This test would need mock data for User/Team/Department relationships
        # For now, test the basic validation logic
        result = await budget_service.validate_budget_hierarchy(EntityType.ORGANIZATION, "org-123", Decimal("1000.00"), "org-123")
        assert result is True  # Organization budget has no parent constraints

    @pytest.mark.asyncio
    async def test_check_budget_legacy_method(self, budget_service, token_context):
        """Test legacy check_budget method for backwards compatibility."""
        # Create user budget
        budget_request = BudgetCreateRequest(
            entity_type=EntityType.USER,
            entity_id="user-123",
            period_type=PeriodType.MONTHLY,
            budget_amount_usd=Decimal("100.00"),
            enforcement_mode=EnforcementMode.HARD,
        )
        await budget_service.create_budget(budget_request, "org-123")

        result = await budget_service.check_budget(token_context)

        assert result.allowed is True
        assert result.exceeded_level is None
        assert result.exceeded_entity is None
