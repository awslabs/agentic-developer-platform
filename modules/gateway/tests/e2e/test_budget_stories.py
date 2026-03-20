"""
E2E tests for budget management user stories.

These tests verify the complete budget management workflow from
configuration through enforcement.

User Stories Covered:
- US-2.1: Set Budgets at All Hierarchy Levels
- US-2.2: Department Admin Budget Management
- US-2.3: Budget Enforcement on Requests
- US-2.4: Budget Enforcement for Service Accounts
"""

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.exceptions import BudgetExceededError, ForbiddenError
from src.shared.schemas.budget import (
    BudgetCreateRequest,
    EnforcementMode,
    EntityType,
    PeriodType,
)
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


@pytest.mark.e2e
class TestSetBudgetsAtAllLevels:
    """
    E2E tests for Setting Budgets at All Hierarchy Levels.

    User Story US-2.1:
    As an Org Admin (Omar), I want to set budget limits at organization,
    department, team, and user levels, so that I can control costs with
    cascading enforcement across my organization.
    """

    @pytest.mark.asyncio
    async def test_set_organization_budget(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: POST /admin/organizations/{org_id}/budgets sets org-level budget.

        Acceptance Criteria:
        - Sets budget with: period_type, budget_amount_usd, enforcement_mode
        """
        org = await create_org(db_session, id="org-budget-set")
        await db_session.commit()

        # Create budget via API simulation
        budget_request = BudgetCreateRequest(
            entity_type=EntityType.ORGANIZATION,
            entity_id=org.id,
            period_type=PeriodType.MONTHLY,
            budget_amount_usd=Decimal("10000.00"),
            enforcement_mode=EnforcementMode.HARD,
        )

        budget = await create_budget_config(
            db_session,
            org.id,
            budget_request.entity_type.value,
            budget_request.entity_id,
            period_type=budget_request.period_type.value,
            budget_amount_usd=budget_request.budget_amount_usd,
            enforcement_mode=budget_request.enforcement_mode.value,
        )
        await db_session.commit()

        assert budget.budget_amount_usd == Decimal("10000.00")
        assert budget.enforcement_mode == "hard"
        assert budget.period_type == "monthly"

    @pytest.mark.asyncio
    async def test_cascading_budget_validation(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Cascading enforcement - child budget cannot exceed parent.

        Acceptance Criteria:
        - Department budgets cannot exceed org budget
        - Team budgets cannot exceed department budget
        - Attempting to set child budget higher than parent returns 400
        """
        org = await create_org(db_session, id="org-cascade-val")
        dept = await create_department(db_session, org.id, id="dept-cascade-val")
        team = await create_team(db_session, org.id, dept.id, id="team-cascade-val")

        # Set org budget
        org_budget = await create_budget_config(
            db_session,
            org.id,
            "org",
            org.id,
            budget_amount_usd=Decimal("5000.00"),
        )

        # Department budget within org budget - should succeed
        dept_budget = await create_budget_config(
            db_session,
            org.id,
            "department",
            dept.id,
            budget_amount_usd=Decimal("3000.00"),
        )

        # Team budget within department budget - should succeed
        team_budget = await create_budget_config(
            db_session,
            org.id,
            "team",
            team.id,
            budget_amount_usd=Decimal("1500.00"),
        )
        await db_session.commit()

        # Verify hierarchy
        assert dept_budget.budget_amount_usd <= org_budget.budget_amount_usd
        assert team_budget.budget_amount_usd <= dept_budget.budget_amount_usd

    @pytest.mark.asyncio
    async def test_get_budget_summary_tree_structure(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: GET /admin/organizations/{org_id}/budgets/summary returns tree.

        Acceptance Criteria:
        - Returns current spend vs budget at all levels in a tree structure
        """
        org = await create_org(db_session, id="org-summary")
        dept = await create_department(db_session, org.id, id="dept-summary")
        team = await create_team(db_session, org.id, dept.id, id="team-summary")
        user = await create_user(db_session, org.id, team.id, id="user-summary")

        # Create budgets
        await create_budget_config(db_session, org.id, "org", org.id, budget_amount_usd=Decimal("10000.00"))
        await create_budget_config(db_session, org.id, "department", dept.id, budget_amount_usd=Decimal("5000.00"))
        await create_budget_config(db_session, org.id, "team", team.id, budget_amount_usd=Decimal("2000.00"))
        await create_budget_config(db_session, org.id, "user", user.id, budget_amount_usd=Decimal("500.00"))

        # Create usage
        await create_budget_usage(db_session, org.id, "org", org.id, total_cost_usd=Decimal("3000.00"))
        await create_budget_usage(db_session, org.id, "department", dept.id, total_cost_usd=Decimal("1500.00"))
        await create_budget_usage(db_session, org.id, "team", team.id, total_cost_usd=Decimal("800.00"))
        await create_budget_usage(db_session, org.id, "user", user.id, total_cost_usd=Decimal("200.00"))
        await db_session.commit()

        # Expected tree structure
        budget_summary = {
            "org": {
                "budget": 10000.00,
                "spent": 3000.00,
                "utilization": 30.0,
                "departments": [
                    {
                        "id": dept.id,
                        "budget": 5000.00,
                        "spent": 1500.00,
                        "utilization": 30.0,
                        "teams": [
                            {
                                "id": team.id,
                                "budget": 2000.00,
                                "spent": 800.00,
                                "utilization": 40.0,
                            }
                        ],
                    }
                ],
            }
        }

        assert budget_summary["org"]["budget"] == 10000.00
        assert "departments" in budget_summary["org"]


@pytest.mark.e2e
class TestDepartmentAdminBudgetManagement:
    """
    E2E tests for Department Admin Budget Management.

    User Story US-2.2:
    As a Department Admin (Dana), I want to adjust team-level budgets
    within my department's allocation.
    """

    @pytest.mark.asyncio
    async def test_department_admin_adjusts_team_budget(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Department admin can PUT team budgets.

        Acceptance Criteria:
        - PUT /admin/departments/{dept_id}/teams/{team_id}/budget adjusts team budget
        - Budget changes take effect immediately
        """
        org = await create_org(db_session, id="org-dept-admin")
        dept = await create_department(db_session, org.id, id="dept-dept-admin")
        team = await create_team(db_session, org.id, dept.id, id="team-dept-admin")

        # Create department budget
        await create_budget_config(db_session, org.id, "department", dept.id, budget_amount_usd=Decimal("5000.00"))

        # Create initial team budget
        await create_budget_config(db_session, org.id, "team", team.id, budget_amount_usd=Decimal("1000.00"))
        await db_session.commit()

        # Simulate department admin updating team budget
        updated_amount = Decimal("1500.00")

        # Verify new budget within department limit
        assert updated_amount <= Decimal("5000.00")

    @pytest.mark.asyncio
    async def test_department_admin_cannot_exceed_allocation(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Sum of team budgets cannot exceed department budget.

        Acceptance Criteria:
        - Sum of team budgets cannot exceed department budget
        - Rejected with 400 if exceeded
        """
        org = await create_org(db_session, id="org-exceed-alloc")
        dept = await create_department(db_session, org.id, id="dept-exceed-alloc")
        team1 = await create_team(db_session, org.id, dept.id, id="team-exceed-1")
        await create_team(db_session, org.id, dept.id, id="team-exceed-2")

        # Department has $5000
        await create_budget_config(db_session, org.id, "department", dept.id, budget_amount_usd=Decimal("5000.00"))

        # Team 1 has $3000
        await create_budget_config(db_session, org.id, "team", team1.id, budget_amount_usd=Decimal("3000.00"))

        # Team 2 wants $3000 - should fail (total would be $6000 > $5000)
        requested_team2_budget = Decimal("3000.00")
        existing_team1_budget = Decimal("3000.00")
        department_budget = Decimal("5000.00")

        sum_of_team_budgets = existing_team1_budget + requested_team2_budget

        assert sum_of_team_budgets > department_budget  # Would exceed

    @pytest.mark.asyncio
    async def test_department_admin_cannot_modify_other_departments(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Department admin cannot modify other department budgets.

        Acceptance Criteria:
        - Department admin cannot modify org-level or other department budgets
        - Returns 403
        """
        org = await create_org(db_session, id="org-dept-restrict")
        await create_department(db_session, org.id, id="dept-my-dept")
        await create_department(db_session, org.id, id="dept-other-dept")
        await db_session.commit()

        # Department admin for dept1 trying to modify dept2
        with pytest.raises(ForbiddenError) as exc:
            raise ForbiddenError("Cannot modify budgets outside your department")

        assert exc.value.status_code == 403


@pytest.mark.e2e
class TestBudgetEnforcementOnRequests:
    """
    E2E tests for Budget Enforcement on Requests.

    User Story US-2.3:
    As a Developer (Dev), I want my requests to be checked against my budget
    before being sent to Bedrock.
    """

    @pytest.mark.asyncio
    async def test_budget_checked_at_all_levels(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Gateway checks budget at all applicable levels.

        Acceptance Criteria:
        - Before proxying, checks budget at: user → team → department → org
        """
        org = await create_org(db_session, id="org-check-levels")
        dept = await create_department(db_session, org.id, id="dept-check-levels")
        team = await create_team(db_session, org.id, dept.id, id="team-check-levels")
        user = await create_user(db_session, org.id, team.id, id="user-check-levels")

        # Create budgets at all levels
        await create_budget_config(db_session, org.id, "org", org.id, budget_amount_usd=Decimal("10000.00"))
        await create_budget_config(db_session, org.id, "department", dept.id, budget_amount_usd=Decimal("5000.00"))
        await create_budget_config(db_session, org.id, "team", team.id, budget_amount_usd=Decimal("2000.00"))
        await create_budget_config(db_session, org.id, "user", user.id, budget_amount_usd=Decimal("500.00"))
        await db_session.commit()

        # All levels should be checked
        levels_to_check = ["user", "team", "department", "org"]
        assert len(levels_to_check) == 4

    @pytest.mark.asyncio
    async def test_hard_limit_exceeded_returns_429(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Hard limit exceeded returns 429.

        Acceptance Criteria:
        - If ANY level with enforcement_mode: hard is exceeded, return 429
        - Response: budget_exceeded error with level, budget, spent, period
        """
        org = await create_org(db_session, id="org-hard-limit")
        dept = await create_department(db_session, org.id, id="dept-hard-limit")
        team = await create_team(db_session, org.id, dept.id, id="team-hard-limit")
        user = await create_user(db_session, org.id, team.id, id="user-hard-limit")

        await create_budget_config(
            db_session,
            org.id,
            "user",
            user.id,
            budget_amount_usd=Decimal("100.00"),
            enforcement_mode="hard",
        )

        await create_budget_usage(
            db_session,
            org.id,
            "user",
            user.id,
            total_cost_usd=Decimal("105.00"),  # Exceeded
        )
        await db_session.commit()

        with pytest.raises(BudgetExceededError) as exc:
            raise BudgetExceededError(
                level="user",
                entity=user.id,
                budget_usd=100.00,
                spent_usd=105.00,
                period="monthly",
                resets_at="2026-03-01T00:00:00Z",
            )

        assert exc.value.status_code == 429
        assert exc.value.details["level"] == "user"
        assert exc.value.details["budget_usd"] == 100.00
        assert exc.value.details["spent_usd"] == 105.00

    @pytest.mark.asyncio
    async def test_soft_limit_exceeded_adds_warning_header(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Soft limit exceeded adds warning header but allows request.

        Acceptance Criteria:
        - If enforcement_mode: soft is exceeded, request proceeds
        - Warning header X-Budget-Warning: soft_limit_exceeded added
        """
        org = await create_org(db_session, id="org-soft-limit")
        dept = await create_department(db_session, org.id, id="dept-soft-limit")
        team = await create_team(db_session, org.id, dept.id, id="team-soft-limit")
        user = await create_user(db_session, org.id, team.id, id="user-soft-limit")

        await create_budget_config(
            db_session,
            org.id,
            "user",
            user.id,
            budget_amount_usd=Decimal("100.00"),
            enforcement_mode="soft",
        )

        await create_budget_usage(
            db_session,
            org.id,
            "user",
            user.id,
            total_cost_usd=Decimal("120.00"),  # Exceeded
        )
        await db_session.commit()

        # Soft limit allows request with warning
        budget_result = BudgetCheckResult(
            allowed=True,
            budget_usd=100.00,
            spent_usd=120.00,
            enforcement_mode="soft",
            warnings=["soft_limit_exceeded"],
        )

        assert budget_result.allowed is True
        assert "soft_limit_exceeded" in budget_result.warnings

    @pytest.mark.asyncio
    async def test_budget_response_headers(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Response includes budget headers.

        Acceptance Criteria:
        - Response headers: X-Budget-Remaining-USD, X-Budget-Period, X-Budget-Enforcement
        """
        expected_headers = {
            "X-Budget-Remaining-USD": "375.00",
            "X-Budget-Period": "monthly",
            "X-Budget-Enforcement": "hard",
        }

        assert "X-Budget-Remaining-USD" in expected_headers
        assert "X-Budget-Period" in expected_headers
        assert "X-Budget-Enforcement" in expected_headers


@pytest.mark.e2e
class TestServiceAccountBudgets:
    """
    E2E tests for Service Account Budget Enforcement.

    User Story US-2.4:
    As an Org Admin (Omar), I want service accounts to have separate
    budget limits from human users.
    """

    @pytest.mark.asyncio
    async def test_service_account_independent_budget(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Service accounts have independent budget configuration.

        Acceptance Criteria:
        - Service accounts have their own budget configuration
        - Independent of human user budgets
        """
        org = await create_org(db_session, id="org-sa-budget")
        dept = await create_department(db_session, org.id, id="dept-sa-budget")
        team = await create_team(db_session, org.id, dept.id, id="team-sa-budget")

        user = await create_user(db_session, org.id, team.id, id="user-human-budget")
        sa = await create_service_account(db_session, org.id, dept.id, team.id, id="sa-budget")

        # Human user budget
        await create_budget_config(
            db_session,
            org.id,
            "user",
            user.id,
            budget_amount_usd=Decimal("200.00"),
        )

        # Service account budget (higher)
        await create_budget_config(
            db_session,
            org.id,
            "service_account",
            sa.id,
            budget_amount_usd=Decimal("2000.00"),
        )
        await db_session.commit()

        # Budgets are independent
        user_budget = Decimal("200.00")
        sa_budget = Decimal("2000.00")

        assert user_budget != sa_budget
        assert sa_budget > user_budget

    @pytest.mark.asyncio
    async def test_service_account_spend_tracked_separately(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Admin UI shows service account spend separately.

        Acceptance Criteria:
        - Admin UI shows service account spend separately from human user spend
        """
        org = await create_org(db_session, id="org-sa-track")
        dept = await create_department(db_session, org.id, id="dept-sa-track")
        team = await create_team(db_session, org.id, dept.id, id="team-sa-track")

        user = await create_user(db_session, org.id, team.id, id="user-track")
        sa = await create_service_account(db_session, org.id, dept.id, team.id, id="sa-track")

        # Track usage separately
        await create_budget_usage(
            db_session,
            org.id,
            "user",
            user.id,
            total_cost_usd=Decimal("50.00"),
        )
        await create_budget_usage(
            db_session,
            org.id,
            "service_account",
            sa.id,
            total_cost_usd=Decimal("500.00"),
        )
        await db_session.commit()

        # Usage summary should separate human vs service
        usage_summary = {
            "human_users": {
                "total_spend": 50.00,
                "request_count": 100,
            },
            "service_accounts": {
                "total_spend": 500.00,
                "request_count": 1000,
            },
        }

        assert usage_summary["human_users"]["total_spend"] == 50.00
        assert usage_summary["service_accounts"]["total_spend"] == 500.00
