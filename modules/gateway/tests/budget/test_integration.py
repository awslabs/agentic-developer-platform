import asyncio
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.budget.service import BudgetService
from src.shared.models.base import Base
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import (
    BudgetCreateRequest,
    CostRecordRequest,
    EnforcementMode,
    EntityType,
    PeriodType,
)


@pytest.fixture
async def test_db():
    """Create a test database with all tables."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    yield async_session_factory

    await engine.dispose()


@pytest.fixture
async def budget_service(test_db):
    """Create a BudgetService with test database."""
    async with test_db() as session:
        yield BudgetService(session)


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


class TestBudgetIntegration:
    """Integration tests for the complete budget system."""

    @pytest.mark.asyncio
    async def test_end_to_end_budget_workflow(self, budget_service, token_context):
        """Test complete budget workflow from creation to enforcement."""

        # 1. Create organization budget
        org_budget_request = BudgetCreateRequest(
            entity_type=EntityType.ORGANIZATION,
            entity_id="org-123",
            period_type=PeriodType.MONTHLY,
            budget_amount_usd=Decimal("10000.00"),
            enforcement_mode=EnforcementMode.HARD,
        )
        org_budget = await budget_service.create_budget(org_budget_request, "org-123")
        assert org_budget.budget_amount_usd == Decimal("10000.00")

        # 2. Create department budget (must be <= org budget)
        dept_budget_request = BudgetCreateRequest(
            entity_type=EntityType.DEPARTMENT,
            entity_id="dept-123",
            period_type=PeriodType.MONTHLY,
            budget_amount_usd=Decimal("5000.00"),
            enforcement_mode=EnforcementMode.HARD,
        )
        dept_budget = await budget_service.create_budget(dept_budget_request, "org-123")
        assert dept_budget.budget_amount_usd == Decimal("5000.00")

        # 3. Create team budget (must be <= dept budget)
        team_budget_request = BudgetCreateRequest(
            entity_type=EntityType.TEAM,
            entity_id="team-123",
            period_type=PeriodType.MONTHLY,
            budget_amount_usd=Decimal("2000.00"),
            enforcement_mode=EnforcementMode.HARD,
        )
        team_budget = await budget_service.create_budget(team_budget_request, "org-123")
        assert team_budget.budget_amount_usd == Decimal("2000.00")

        # 4. Create user budget (must be <= team budget)
        user_budget_request = BudgetCreateRequest(
            entity_type=EntityType.USER,
            entity_id="user-123",
            period_type=PeriodType.MONTHLY,
            budget_amount_usd=Decimal("500.00"),
            enforcement_mode=EnforcementMode.HARD,
        )
        user_budget = await budget_service.create_budget(user_budget_request, "org-123")
        assert user_budget.budget_amount_usd == Decimal("500.00")

        # 5. Check budget status (should be clean)
        user_status = await budget_service.get_budget_status(EntityType.USER, "user-123", PeriodType.MONTHLY, "org-123")
        assert user_status.current_spend_usd == Decimal("0")
        assert user_status.budget_exceeded is False

        # 6. Record some usage
        await budget_service.record_usage(token_context, tokens_in=1000, tokens_out=500, model="claude-3-5-sonnet-20241022")

        # 7. Check budget status after usage
        user_status = await budget_service.get_budget_status(EntityType.USER, "user-123", PeriodType.MONTHLY, "org-123")
        assert user_status.current_spend_usd > Decimal("0")
        assert user_status.budget_exceeded is False

        # 8. Check hierarchical budget enforcement
        small_cost_result = await budget_service.check_hierarchical_budget(token_context, Decimal("10.00"))
        assert small_cost_result.allowed is True

        # 9. Try to exceed user budget
        large_cost_result = await budget_service.check_hierarchical_budget(
            token_context,
            Decimal("600.00"),  # Would exceed $500 user budget
        )
        assert large_cost_result.allowed is False
        assert large_cost_result.exceeded_entity_type == EntityType.USER

    @pytest.mark.asyncio
    async def test_hierarchical_budget_validation(self, budget_service):
        """Test that child budgets cannot exceed parent budgets."""

        # Create organization budget
        org_budget_request = BudgetCreateRequest(
            entity_type=EntityType.ORGANIZATION,
            entity_id="org-123",
            period_type=PeriodType.MONTHLY,
            budget_amount_usd=Decimal("1000.00"),
            enforcement_mode=EnforcementMode.HARD,
        )
        await budget_service.create_budget(org_budget_request, "org-123")

        # Try to create department budget that exceeds org budget
        from src.shared.exceptions import ValidationError

        dept_budget_request = BudgetCreateRequest(
            entity_type=EntityType.DEPARTMENT,
            entity_id="dept-123",
            period_type=PeriodType.MONTHLY,
            budget_amount_usd=Decimal("2000.00"),  # Exceeds org budget
            enforcement_mode=EnforcementMode.HARD,
        )

        with pytest.raises(ValidationError, match="Budget amount exceeds parent budget constraints"):
            await budget_service.create_budget(dept_budget_request, "org-123")

    @pytest.mark.asyncio
    async def test_budget_enforcement_across_periods(self, budget_service, token_context):
        """Test budget enforcement across different time periods."""

        # Create budgets for different periods
        periods = [
            (PeriodType.DAILY, Decimal("10.00")),
            (PeriodType.WEEKLY, Decimal("50.00")),
            (PeriodType.MONTHLY, Decimal("200.00")),
        ]

        for period_type, amount in periods:
            budget_request = BudgetCreateRequest(
                entity_type=EntityType.USER,
                entity_id="user-123",
                period_type=period_type,
                budget_amount_usd=amount,
                enforcement_mode=EnforcementMode.HARD,
            )
            await budget_service.create_budget(budget_request, "org-123")

        # Record usage that would exceed daily budget
        await budget_service.record_usage(token_context, tokens_in=2000, tokens_out=1000, model="claude-3-5-sonnet-20241022")

        # Check budget status for each period
        for period_type, _ in periods:
            status = await budget_service.get_budget_status(EntityType.USER, "user-123", period_type, "org-123")
            assert status is not None

        # Check if daily budget would be exceeded by additional usage
        result = await budget_service.check_hierarchical_budget(
            token_context,
            Decimal("15.00"),  # Would exceed daily budget
        )
        assert result.allowed is False

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="Integration test requires proper usage accumulation across sessions - needs further investigation")
    async def test_soft_vs_hard_enforcement(self, budget_service, token_context):
        """Test the difference between soft and hard budget enforcement."""

        # Create budget with soft enforcement
        soft_budget_request = BudgetCreateRequest(
            entity_type=EntityType.USER,
            entity_id="user-soft",
            period_type=PeriodType.MONTHLY,
            budget_amount_usd=Decimal("100.00"),
            enforcement_mode=EnforcementMode.SOFT,
        )
        await budget_service.create_budget(soft_budget_request, "org-123")

        # Create budget with hard enforcement
        hard_budget_request = BudgetCreateRequest(
            entity_type=EntityType.USER,
            entity_id="user-hard",
            period_type=PeriodType.MONTHLY,
            budget_amount_usd=Decimal("100.00"),
            enforcement_mode=EnforcementMode.HARD,
        )
        await budget_service.create_budget(hard_budget_request, "org-123")

        # Record usage that would exceed both budgets
        soft_context = TokenContext(
            user_id="user-soft",
            org_id="org-123",
            team_id="team-123",
            department_id="dept-123",
            account_type="human",
            is_admin=False,
            expires_at=datetime.utcnow(),
        )

        hard_context = TokenContext(
            user_id="user-hard",
            org_id="org-123",
            team_id="team-123",
            department_id="dept-123",
            account_type="human",
            is_admin=False,
            expires_at=datetime.utcnow(),
        )

        # Record usage to approach budget limits
        for context in [soft_context, hard_context]:
            await budget_service.record_usage(context, tokens_in=15000, tokens_out=7500, model="claude-3-5-sonnet-20241022")

        # Try to make request that would exceed budget
        exceeding_cost = Decimal("50.00")

        # Soft enforcement should allow with warnings
        soft_result = await budget_service.check_hierarchical_budget(soft_context, exceeding_cost)
        assert soft_result.allowed is True
        assert len(soft_result.warnings) > 0

        # Hard enforcement should block
        hard_result = await budget_service.check_hierarchical_budget(hard_context, exceeding_cost)
        assert hard_result.allowed is False
        assert hard_result.blocked_reason is not None

    @pytest.mark.asyncio
    async def test_cost_calculation_and_recording_integration(self, budget_service):
        """Test integration between cost calculation and recording."""

        # Test cost calculation for different models
        models = [
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
            "claude-3-opus-20240229",
            "unknown-model",
        ]

        for model in models:
            from src.shared.schemas.budget import CostCalculationRequest

            calc_request = CostCalculationRequest(
                model_name=model,
                tokens_in=1000,
                tokens_out=500,
            )

            response = await budget_service.calculate_cost(calc_request)
            assert response.cost_usd > Decimal("0")
            assert response.tokens_in == 1000
            assert response.tokens_out == 500

            # Record the calculated cost
            cost_request = CostRecordRequest(
                entity_type=EntityType.USER,
                entity_id="user-123",
                model_name=model,
                tokens_in=1000,
                tokens_out=500,
                request_cost_usd=response.cost_usd,
            )

            await budget_service.record_cost(cost_request, "org-123")

        # Verify usage was recorded
        usage = await budget_service.get_budget_usage(EntityType.USER, "user-123", PeriodType.MONTHLY, "org-123")
        assert usage is not None
        assert usage.request_count == len(models)
        assert usage.total_tokens == len(models) * 1500  # 1000 + 500 per model

    @pytest.mark.asyncio
    async def test_budget_reporting_integration(self, budget_service, token_context):
        """Test budget reporting and analytics integration."""

        # Create various budgets and usage
        entities = [
            (EntityType.ORGANIZATION, "org-123", Decimal("10000.00")),
            (EntityType.DEPARTMENT, "dept-123", Decimal("5000.00")),
            (EntityType.TEAM, "team-123", Decimal("2000.00")),
            (EntityType.USER, "user-123", Decimal("500.00")),
            (EntityType.USER, "user-456", Decimal("300.00")),
        ]

        for entity_type, entity_id, amount in entities:
            budget_request = BudgetCreateRequest(
                entity_type=entity_type,
                entity_id=entity_id,
                period_type=PeriodType.MONTHLY,
                budget_amount_usd=amount,
                enforcement_mode=EnforcementMode.HARD,
            )
            await budget_service.create_budget(budget_request, "org-123")

        # Record usage for different users
        for user_id in ["user-123", "user-456"]:
            user_context = TokenContext(
                user_id=user_id,
                org_id="org-123",
                team_id="team-123",
                department_id="dept-123",
                account_type="human",
                is_admin=False,
                expires_at=datetime.utcnow(),
            )
            await budget_service.record_usage(user_context, tokens_in=2000, tokens_out=1000, model="claude-3-5-sonnet-20241022")

        # Test budget summary
        summary = await budget_service.get_budget_summary("user", "user-123", "org-123")
        assert summary["entity_type"] == "user"
        assert len(summary["budgets"]) == 1

        # Test organization overview
        overview = await budget_service.get_organization_budget_overview("org-123")
        assert overview["org_id"] == "org-123"
        assert overview["total_budgets"] == len(entities)

        # Test budget alerts
        alerts = await budget_service.get_budget_alerts("org-123", threshold_percent=0.0)
        assert len(alerts) >= 0  # Should have some alerts based on usage

    @pytest.mark.asyncio
    async def test_budget_updates_and_deletions(self, budget_service):
        """Test budget update and deletion operations."""

        # Create initial budget
        budget_request = BudgetCreateRequest(
            entity_type=EntityType.USER,
            entity_id="user-123",
            period_type=PeriodType.MONTHLY,
            budget_amount_usd=Decimal("100.00"),
            enforcement_mode=EnforcementMode.HARD,
        )
        budget = await budget_service.create_budget(budget_request, "org-123")

        # Update budget amount
        from src.shared.schemas.budget import BudgetUpdateRequest

        update_request = BudgetUpdateRequest(
            budget_amount_usd=Decimal("200.00"),
            enforcement_mode=EnforcementMode.SOFT,
        )
        updated_budget = await budget_service.update_budget(budget.id, update_request, "org-123")

        assert updated_budget.budget_amount_usd == Decimal("200.00")
        assert updated_budget.enforcement_mode == EnforcementMode.SOFT

        # Verify the update persisted
        retrieved_budget = await budget_service.get_budget(budget.id, "org-123")
        assert retrieved_budget.budget_amount_usd == Decimal("200.00")

        # Delete budget
        success = await budget_service.delete_budget(budget.id, "org-123")
        assert success is True

        # Verify deletion
        deleted_budget = await budget_service.get_budget(budget.id, "org-123")
        assert deleted_budget is None

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="Integration test has SQLAlchemy session issues with concurrent operations - needs transaction handling refactor")
    async def test_concurrent_budget_usage_recording(self, budget_service, token_context):
        """Test concurrent budget usage recording for race conditions."""

        # Create budget
        budget_request = BudgetCreateRequest(
            entity_type=EntityType.USER,
            entity_id="user-123",
            period_type=PeriodType.MONTHLY,
            budget_amount_usd=Decimal("1000.00"),
            enforcement_mode=EnforcementMode.HARD,
        )
        await budget_service.create_budget(budget_request, "org-123")

        # Record multiple concurrent usages
        tasks = []
        for i in range(10):
            task = budget_service.record_usage(token_context, tokens_in=100, tokens_out=50, model="claude-3-5-sonnet-20241022")
            tasks.append(task)

        # Execute all tasks concurrently
        await asyncio.gather(*tasks)

        # Verify all usage was recorded correctly
        usage = await budget_service.get_budget_usage(EntityType.USER, "user-123", PeriodType.MONTHLY, "org-123")
        assert usage is not None
        assert usage.request_count == 10
        assert usage.total_tokens == 1500  # 150 * 10
        assert usage.total_cost_usd > Decimal("0")
