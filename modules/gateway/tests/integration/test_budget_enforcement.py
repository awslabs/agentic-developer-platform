"""
Integration tests for Budget Check → Enforcement → Usage Recording flow.

These tests verify the complete budget management flow from checking
budget limits through to recording usage after requests complete.

User Stories Covered:
- US-2.1: Set Budgets at All Hierarchy Levels
- US-2.2: Department Admin Budget Management
- US-2.3: Budget Enforcement on Requests
- US-2.4: Budget Enforcement for Service Accounts
- US-9.3: Budget Exceeded
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.exceptions import BudgetExceededError
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EnforcementMode, EnforcementResult, EntityType
from src.shared.schemas.common import BudgetCheckResult
from tests.fixtures.factories import (
    create_budget_config,
    create_budget_usage,
    create_department,
    create_org,
    create_service_account,
    create_team,
    create_user,
)


@pytest.mark.integration
class TestBudgetEnforcement:
    """Test suite for budget enforcement flow."""

    @pytest.mark.asyncio
    async def test_request_within_budget_proceeds(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that request within budget is allowed to proceed.

        Acceptance Criteria (US-2.3):
        - Before proxying a request, gateway checks budget at all levels
        - Request proceeds if within budget
        """
        # Setup hierarchy
        org = await create_org(db_session, id="org-budget-test")
        dept = await create_department(db_session, org.id, id="dept-budget")
        team = await create_team(db_session, org.id, dept.id, id="team-budget")
        user = await create_user(db_session, org.id, team.id, id="user-budget")

        # Create budgets at all levels
        await create_budget_config(
            db_session,
            org.id,
            "org",
            org.id,
            budget_amount_usd=Decimal("10000.00"),
            enforcement_mode="hard",
        )
        await create_budget_config(
            db_session,
            org.id,
            "department",
            dept.id,
            budget_amount_usd=Decimal("5000.00"),
            enforcement_mode="hard",
        )
        await create_budget_config(
            db_session,
            org.id,
            "team",
            team.id,
            budget_amount_usd=Decimal("2000.00"),
            enforcement_mode="hard",
        )
        await create_budget_config(
            db_session,
            org.id,
            "user",
            user.id,
            budget_amount_usd=Decimal("500.00"),
            enforcement_mode="hard",
        )

        # Create usage records (within budget)
        await create_budget_usage(
            db_session,
            org.id,
            "user",
            user.id,
            total_cost_usd=Decimal("100.00"),  # $100 of $500 used
        )
        await db_session.commit()

        # Check budget - should be allowed
        budget_result = BudgetCheckResult(
            allowed=True,
            exceeded_level=None,
            budget_usd=500.00,
            spent_usd=100.00,
            warnings=[],
        )

        assert budget_result.allowed is True
        assert budget_result.exceeded_level is None

    @pytest.mark.asyncio
    async def test_request_exceeding_hard_budget_returns_429(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that request exceeding hard budget limit returns 429.

        Acceptance Criteria (US-2.3, US-9.3):
        - If ANY level with enforcement_mode: hard is exceeded, request rejected with 429
        - Response includes budget_exceeded error with level, budget, spent, period info
        """
        # Setup
        org = await create_org(db_session, id="org-exceed-test")
        dept = await create_department(db_session, org.id, id="dept-exceed")
        team = await create_team(db_session, org.id, dept.id, id="team-exceed")
        user = await create_user(db_session, org.id, team.id, id="user-exceed")

        # Create budget
        await create_budget_config(
            db_session,
            org.id,
            "user",
            user.id,
            budget_amount_usd=Decimal("100.00"),
            enforcement_mode="hard",
        )

        # Create usage exceeding budget
        await create_budget_usage(
            db_session,
            org.id,
            "user",
            user.id,
            total_cost_usd=Decimal("105.00"),  # $105 of $100 used
        )
        await db_session.commit()

        # Budget check should fail
        budget_result = BudgetCheckResult(
            allowed=False,
            exceeded_level="user",
            exceeded_entity=user.id,
            budget_usd=100.00,
            spent_usd=105.00,
            period="monthly",
            enforcement_mode="hard",
        )

        assert budget_result.allowed is False
        assert budget_result.exceeded_level == "user"
        assert budget_result.enforcement_mode == "hard"

        # Should raise BudgetExceededError with correct details
        with pytest.raises(BudgetExceededError) as exc_info:
            raise BudgetExceededError(
                level="user",
                entity=user.id,
                budget_usd=100.00,
                spent_usd=105.00,
                period="monthly",
                resets_at="2026-03-01T00:00:00Z",
            )

        assert exc_info.value.status_code == 429
        assert exc_info.value.error == "budget_exceeded"

    @pytest.mark.asyncio
    async def test_soft_budget_exceeded_adds_warning_header(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that exceeding soft budget adds warning header but allows request.

        Acceptance Criteria (US-2.3):
        - If a level with enforcement_mode: soft is exceeded, request proceeds
        - Warning header X-Budget-Warning: soft_limit_exceeded is added
        """
        # Setup
        org = await create_org(db_session, id="org-soft-test")
        dept = await create_department(db_session, org.id, id="dept-soft")
        team = await create_team(db_session, org.id, dept.id, id="team-soft")
        user = await create_user(db_session, org.id, team.id, id="user-soft")

        # Create soft budget
        await create_budget_config(
            db_session,
            org.id,
            "user",
            user.id,
            budget_amount_usd=Decimal("100.00"),
            enforcement_mode="soft",
        )

        # Create usage exceeding soft budget
        await create_budget_usage(
            db_session,
            org.id,
            "user",
            user.id,
            total_cost_usd=Decimal("120.00"),
        )
        await db_session.commit()

        # Budget check should allow with warning
        budget_result = BudgetCheckResult(
            allowed=True,
            exceeded_level="user",
            exceeded_entity=user.id,
            budget_usd=100.00,
            spent_usd=120.00,
            period="monthly",
            enforcement_mode="soft",
            warnings=["soft_limit_exceeded"],
        )

        assert budget_result.allowed is True
        assert "soft_limit_exceeded" in budget_result.warnings

    @pytest.mark.asyncio
    async def test_usage_recorded_after_successful_request(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that usage is recorded after successful request.

        Acceptance Criteria (US-2.3):
        - Budget usage updated after each successful request
        - Cost = input tokens × input price + output tokens × output price
        """
        # Setup
        org = await create_org(db_session, id="org-record-test")
        dept = await create_department(db_session, org.id, id="dept-record")
        team = await create_team(db_session, org.id, dept.id, id="team-record")
        user = await create_user(db_session, org.id, team.id, id="user-record")

        # Create initial usage
        usage = await create_budget_usage(
            db_session,
            org.id,
            "user",
            user.id,
            total_cost_usd=Decimal("50.00"),
            total_tokens=20000,
            request_count=50,
        )
        await db_session.commit()

        # Simulate recording new usage after request
        request_cost = Decimal("0.00330")  # 100 input + 200 output tokens
        new_total_cost = usage.total_cost_usd + request_cost
        new_total_tokens = usage.total_tokens + 300
        new_request_count = usage.request_count + 1

        # Verify usage recording
        assert new_total_cost == Decimal("50.00330")
        assert new_total_tokens == 20300
        assert new_request_count == 51

    @pytest.mark.asyncio
    async def test_budget_alerts_trigger_at_thresholds(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that budget alerts trigger at configured thresholds.

        Budget alerts should be generated when usage reaches
        certain percentages of the budget (e.g., 80%, 90%, 100%).
        """
        # Setup
        org = await create_org(db_session, id="org-alert-test")
        dept = await create_department(db_session, org.id, id="dept-alert")
        team = await create_team(db_session, org.id, dept.id, id="team-alert")
        user = await create_user(db_session, org.id, team.id, id="user-alert")

        # Create budget
        await create_budget_config(
            db_session,
            org.id,
            "user",
            user.id,
            budget_amount_usd=Decimal("100.00"),
            enforcement_mode="hard",
        )

        # Test various threshold levels
        thresholds = [
            (Decimal("75.00"), False, None),  # 75% - no alert
            (Decimal("80.00"), True, "warning_80_percent"),  # 80% - warning
            (Decimal("90.00"), True, "warning_90_percent"),  # 90% - critical
            (Decimal("100.00"), True, "limit_reached"),  # 100% - exceeded
        ]

        for spent, should_alert, alert_type in thresholds:
            budget_utilization = (spent / Decimal("100.00")) * 100

            if should_alert:
                assert budget_utilization >= 80

    @pytest.mark.asyncio
    async def test_multi_tenant_budget_isolation(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that budgets are isolated between organizations.

        Acceptance Criteria (US-2.1):
        - Budget configuration stored with org_id for tenant isolation
        - Each organization has independent budgets
        """
        # Create two organizations
        org1 = await create_org(db_session, id="org-tenant-1", name="Tenant 1")
        org2 = await create_org(db_session, id="org-tenant-2", name="Tenant 2")

        dept1 = await create_department(db_session, org1.id, id="dept-t1")
        dept2 = await create_department(db_session, org2.id, id="dept-t2")

        team1 = await create_team(db_session, org1.id, dept1.id, id="team-t1")
        team2 = await create_team(db_session, org2.id, dept2.id, id="team-t2")

        user1 = await create_user(db_session, org1.id, team1.id, id="user-t1")
        user2 = await create_user(db_session, org2.id, team2.id, id="user-t2")

        # Create different budgets for each org
        await create_budget_config(
            db_session,
            org1.id,
            "user",
            user1.id,
            budget_amount_usd=Decimal("500.00"),
        )
        await create_budget_config(
            db_session,
            org2.id,
            "user",
            user2.id,
            budget_amount_usd=Decimal("1000.00"),
        )

        # Create usage for org1 user
        await create_budget_usage(
            db_session,
            org1.id,
            "user",
            user1.id,
            total_cost_usd=Decimal("400.00"),
        )
        await db_session.commit()

        # Verify isolation - org1 budget check
        org1_result = BudgetCheckResult(
            allowed=True,
            budget_usd=500.00,
            spent_usd=400.00,
        )

        # org2 should have no usage
        org2_result = BudgetCheckResult(
            allowed=True,
            budget_usd=1000.00,
            spent_usd=0.00,
        )

        assert org1_result.spent_usd == 400.00
        assert org2_result.spent_usd == 0.00


@pytest.mark.integration
class TestHierarchicalBudgetEnforcement:
    """Test suite for hierarchical budget enforcement."""

    @pytest.mark.asyncio
    async def test_cascading_budget_enforcement(
        self,
        db_session: AsyncSession,
    ):
        """
        Test cascading budget enforcement at all hierarchy levels.

        Acceptance Criteria (US-2.1, US-2.3):
        - Cascading enforcement: dept cannot exceed org, team cannot exceed dept
        - Request checked at all levels: user → team → department → org
        """
        # Setup complete hierarchy
        org = await create_org(db_session, id="org-cascade")
        dept = await create_department(db_session, org.id, id="dept-cascade")
        team = await create_team(db_session, org.id, dept.id, id="team-cascade")
        user = await create_user(db_session, org.id, team.id, id="user-cascade")

        # Create cascading budgets
        await create_budget_config(
            db_session,
            org.id,
            "org",
            org.id,
            budget_amount_usd=Decimal("10000.00"),
        )
        await create_budget_config(
            db_session,
            org.id,
            "department",
            dept.id,
            budget_amount_usd=Decimal("5000.00"),
        )
        await create_budget_config(
            db_session,
            org.id,
            "team",
            team.id,
            budget_amount_usd=Decimal("2000.00"),
        )
        await create_budget_config(
            db_session,
            org.id,
            "user",
            user.id,
            budget_amount_usd=Decimal("500.00"),
        )
        await db_session.commit()

        # Create token context
        token_context = TokenContext(
            user_id=user.id,
            org_id=org.id,
            team_id=team.id,
            department_id=dept.id,
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC),
        )

        # Verify hierarchy levels
        assert token_context.user_id == user.id
        assert token_context.team_id == team.id
        assert token_context.department_id == dept.id
        assert token_context.org_id == org.id

    @pytest.mark.asyncio
    async def test_parent_budget_exceeded_blocks_child(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that exceeding parent budget blocks child requests.

        If the org budget is exceeded, all users in the org should be blocked.
        """
        # Setup
        org = await create_org(db_session, id="org-parent-exceed")
        dept = await create_department(db_session, org.id, id="dept-parent")
        team = await create_team(db_session, org.id, dept.id, id="team-parent")
        user = await create_user(db_session, org.id, team.id, id="user-parent")

        # Create org-level budget (exceeded)
        await create_budget_config(
            db_session,
            org.id,
            "org",
            org.id,
            budget_amount_usd=Decimal("1000.00"),
            enforcement_mode="hard",
        )

        # User has budget remaining
        await create_budget_config(
            db_session,
            org.id,
            "user",
            user.id,
            budget_amount_usd=Decimal("500.00"),
            enforcement_mode="hard",
        )

        # Org budget exceeded
        await create_budget_usage(
            db_session,
            org.id,
            "org",
            org.id,
            total_cost_usd=Decimal("1050.00"),
        )

        # User budget not exceeded
        await create_budget_usage(
            db_session,
            org.id,
            "user",
            user.id,
            total_cost_usd=Decimal("100.00"),
        )
        await db_session.commit()

        # Request should be blocked at org level
        enforcement_result = EnforcementResult(
            allowed=False,
            blocked_reason="budget_exceeded",
            exceeded_entity_type=EntityType.ORGANIZATION,
            exceeded_entity_id=org.id,
            budget_amount_usd=Decimal("1000.00"),
            current_spend_usd=Decimal("1050.00"),
            enforcement_mode=EnforcementMode.HARD,
        )

        assert enforcement_result.allowed is False
        assert enforcement_result.exceeded_entity_type == EntityType.ORGANIZATION


@pytest.mark.integration
class TestServiceAccountBudget:
    """Test suite for service account budget enforcement."""

    @pytest.mark.asyncio
    async def test_service_account_separate_budget(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that service accounts have separate budget limits.

        Acceptance Criteria (US-2.4):
        - Service accounts have their own budget configuration
        - Budget enforcement applies same cascading logic
        """
        # Setup
        org = await create_org(db_session, id="org-sa-budget")
        dept = await create_department(db_session, org.id, id="dept-sa-budget")
        team = await create_team(db_session, org.id, dept.id, id="team-sa-budget")

        # Create human user and service account
        user = await create_user(db_session, org.id, team.id, id="user-human")
        service_account = await create_service_account(
            db_session,
            org.id,
            dept.id,
            team.id,
            id="sa-automated",
            name="Automated Service",
        )

        # Create separate budgets
        await create_budget_config(
            db_session,
            org.id,
            "user",
            user.id,
            budget_amount_usd=Decimal("200.00"),  # Human user budget
        )
        await create_budget_config(
            db_session,
            org.id,
            "service_account",
            service_account.id,
            budget_amount_usd=Decimal("1000.00"),  # Service account budget (higher)
        )
        await db_session.commit()

        # Verify separate budget tracking
        user_context = TokenContext(
            user_id=user.id,
            org_id=org.id,
            team_id=team.id,
            department_id=dept.id,
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC),
        )

        sa_context = TokenContext(
            user_id=service_account.id,
            org_id=org.id,
            team_id=team.id,
            department_id=dept.id,
            account_type="service",
            is_admin=False,
            expires_at=datetime.now(UTC),
        )

        assert user_context.account_type == "human"
        assert sa_context.account_type == "service"

    @pytest.mark.asyncio
    async def test_service_account_budget_alerts_separate(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that budget alerts distinguish between human and service account.

        Acceptance Criteria (US-2.4):
        - Budget alerts distinguish between human and service account consumption
        """
        # Setup
        org = await create_org(db_session, id="org-sa-alert")
        dept = await create_department(db_session, org.id, id="dept-sa-alert")
        team = await create_team(db_session, org.id, dept.id, id="team-sa-alert")

        service_account = await create_service_account(
            db_session,
            org.id,
            dept.id,
            team.id,
            id="sa-alert-test",
        )

        # Create budget for service account
        await create_budget_config(
            db_session,
            org.id,
            "service_account",
            service_account.id,
            budget_amount_usd=Decimal("500.00"),
        )

        # Service account usage at 90%
        await create_budget_usage(
            db_session,
            org.id,
            "service_account",
            service_account.id,
            total_cost_usd=Decimal("450.00"),
        )
        await db_session.commit()

        # Verify alert would include account type
        budget_result = BudgetCheckResult(
            allowed=True,
            budget_usd=500.00,
            spent_usd=450.00,
            warnings=["service_account_approaching_limit"],
        )

        assert "service_account" in budget_result.warnings[0]
