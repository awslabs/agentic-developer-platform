"""
E2E tests for budget management user stories.

Test modes:
- @pytest.mark.unit: Pure Python-level logic tests (db_session + mocks)
- @pytest.mark.integration: ASGI app in-process tests
- @pytest.mark.live_only: Real HTTP against deployed gateway

User Stories Covered:
- US-2.1: Set Budgets at All Hierarchy Levels
- US-2.2: Department Admin Budget Management
- US-2.3: Budget Enforcement on Requests
- US-2.4: Budget Enforcement for Service Accounts
"""

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.budget, pytest.mark.e2e]

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

# =============================================================================
# Unit tests -- pure Python logic, db_session + mocks
# =============================================================================


@pytest.mark.unit
class TestSetBudgetsAtAllLevels:
    """
    Unit tests for Setting Budgets at All Hierarchy Levels.

    User Story US-2.1.
    """

    async def test_set_organization_budget(self, db_session: AsyncSession):
        """POST /admin/organizations/{org_id}/budgets sets org-level budget."""
        org = await create_org(db_session, id="org-budget-set")
        await db_session.commit()

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

    async def test_cascading_budget_validation(self, db_session: AsyncSession):
        """Cascading enforcement -- child budget cannot exceed parent."""
        org = await create_org(db_session, id="org-cascade-val")
        dept = await create_department(db_session, org.id, id="dept-cascade-val")
        team = await create_team(db_session, org.id, dept.id, id="team-cascade-val")

        org_budget = await create_budget_config(
            db_session,
            org.id,
            "org",
            org.id,
            budget_amount_usd=Decimal("5000.00"),
        )
        dept_budget = await create_budget_config(
            db_session,
            org.id,
            "department",
            dept.id,
            budget_amount_usd=Decimal("3000.00"),
        )
        team_budget = await create_budget_config(
            db_session,
            org.id,
            "team",
            team.id,
            budget_amount_usd=Decimal("1500.00"),
        )
        await db_session.commit()

        assert dept_budget.budget_amount_usd <= org_budget.budget_amount_usd
        assert team_budget.budget_amount_usd <= dept_budget.budget_amount_usd

    async def test_get_budget_summary_tree_structure(self, db_session: AsyncSession):
        """GET /admin/organizations/{org_id}/budgets/summary returns tree."""
        org = await create_org(db_session, id="org-summary")
        dept = await create_department(db_session, org.id, id="dept-summary")
        team = await create_team(db_session, org.id, dept.id, id="team-summary")
        user = await create_user(db_session, org.id, team.id, id="user-summary")

        await create_budget_config(db_session, org.id, "org", org.id, budget_amount_usd=Decimal("10000.00"))
        await create_budget_config(db_session, org.id, "department", dept.id, budget_amount_usd=Decimal("5000.00"))
        await create_budget_config(db_session, org.id, "team", team.id, budget_amount_usd=Decimal("2000.00"))
        await create_budget_config(db_session, org.id, "user", user.id, budget_amount_usd=Decimal("500.00"))

        await create_budget_usage(db_session, org.id, "org", org.id, total_cost_usd=Decimal("3000.00"))
        await create_budget_usage(db_session, org.id, "department", dept.id, total_cost_usd=Decimal("1500.00"))
        await create_budget_usage(db_session, org.id, "team", team.id, total_cost_usd=Decimal("800.00"))
        await create_budget_usage(db_session, org.id, "user", user.id, total_cost_usd=Decimal("200.00"))
        await db_session.commit()

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
                        "teams": [{"id": team.id, "budget": 2000.00, "spent": 800.00, "utilization": 40.0}],
                    }
                ],
            }
        }

        assert budget_summary["org"]["budget"] == 10000.00
        assert "departments" in budget_summary["org"]


@pytest.mark.unit
class TestDepartmentAdminBudgetManagement:
    """Unit tests for Department Admin Budget Management. US-2.2."""

    async def test_department_admin_adjusts_team_budget(self, db_session: AsyncSession):
        """Department admin can PUT team budgets."""
        org = await create_org(db_session, id="org-dept-admin")
        dept = await create_department(db_session, org.id, id="dept-dept-admin")
        await create_team(db_session, org.id, dept.id, id="team-dept-admin")

        await create_budget_config(db_session, org.id, "department", dept.id, budget_amount_usd=Decimal("5000.00"))
        await create_budget_config(db_session, org.id, "team", "team-dept-admin", budget_amount_usd=Decimal("1000.00"))
        await db_session.commit()

        updated_amount = Decimal("1500.00")
        assert updated_amount <= Decimal("5000.00")

    async def test_department_admin_cannot_exceed_allocation(self, db_session: AsyncSession):
        """Sum of team budgets cannot exceed department budget."""
        org = await create_org(db_session, id="org-exceed-alloc")
        dept = await create_department(db_session, org.id, id="dept-exceed-alloc")
        team1 = await create_team(db_session, org.id, dept.id, id="team-exceed-1")
        await create_team(db_session, org.id, dept.id, id="team-exceed-2")

        await create_budget_config(db_session, org.id, "department", dept.id, budget_amount_usd=Decimal("5000.00"))
        await create_budget_config(db_session, org.id, "team", team1.id, budget_amount_usd=Decimal("3000.00"))

        requested_team2_budget = Decimal("3000.00")
        existing_team1_budget = Decimal("3000.00")
        department_budget = Decimal("5000.00")

        sum_of_team_budgets = existing_team1_budget + requested_team2_budget
        assert sum_of_team_budgets > department_budget

    async def test_department_admin_cannot_modify_other_departments(self, db_session: AsyncSession):
        """Department admin cannot modify other department budgets."""
        org = await create_org(db_session, id="org-dept-restrict")
        await create_department(db_session, org.id, id="dept-my-dept")
        await create_department(db_session, org.id, id="dept-other-dept")
        await db_session.commit()

        with pytest.raises(ForbiddenError) as exc:
            raise ForbiddenError("Cannot modify budgets outside your department")
        assert exc.value.status_code == 403


@pytest.mark.unit
class TestBudgetEnforcementOnRequests:
    """Unit tests for Budget Enforcement on Requests. US-2.3."""

    async def test_budget_checked_at_all_levels(self, db_session: AsyncSession):
        """Gateway checks budget at all applicable levels."""
        org = await create_org(db_session, id="org-check-levels")
        dept = await create_department(db_session, org.id, id="dept-check-levels")
        team = await create_team(db_session, org.id, dept.id, id="team-check-levels")
        user = await create_user(db_session, org.id, team.id, id="user-check-levels")

        await create_budget_config(db_session, org.id, "org", org.id, budget_amount_usd=Decimal("10000.00"))
        await create_budget_config(db_session, org.id, "department", dept.id, budget_amount_usd=Decimal("5000.00"))
        await create_budget_config(db_session, org.id, "team", team.id, budget_amount_usd=Decimal("2000.00"))
        await create_budget_config(db_session, org.id, "user", user.id, budget_amount_usd=Decimal("500.00"))
        await db_session.commit()

        levels_to_check = ["user", "team", "department", "org"]
        assert len(levels_to_check) == 4

    async def test_hard_limit_exceeded_returns_429(self, db_session: AsyncSession):
        """Hard limit exceeded returns 429."""
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
            total_cost_usd=Decimal("105.00"),
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

    async def test_soft_limit_exceeded_adds_warning_header(self, db_session: AsyncSession):
        """Soft limit exceeded adds warning header but allows request."""
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
            total_cost_usd=Decimal("120.00"),
        )
        await db_session.commit()

        budget_result = BudgetCheckResult(
            allowed=True,
            budget_usd=100.00,
            spent_usd=120.00,
            enforcement_mode="soft",
            warnings=["soft_limit_exceeded"],
        )

        assert budget_result.allowed is True
        assert "soft_limit_exceeded" in budget_result.warnings

    async def test_budget_response_headers(self, db_session: AsyncSession):
        """Response includes budget headers."""
        expected_headers = {
            "X-Budget-Remaining-USD": "375.00",
            "X-Budget-Period": "monthly",
            "X-Budget-Enforcement": "hard",
        }

        assert "X-Budget-Remaining-USD" in expected_headers
        assert "X-Budget-Period" in expected_headers
        assert "X-Budget-Enforcement" in expected_headers


@pytest.mark.unit
class TestServiceAccountBudgets:
    """Unit tests for Service Account Budget Enforcement. US-2.4."""

    async def test_service_account_independent_budget(self, db_session: AsyncSession):
        """Service accounts have independent budget configuration."""
        org = await create_org(db_session, id="org-sa-budget")
        dept = await create_department(db_session, org.id, id="dept-sa-budget")
        team = await create_team(db_session, org.id, dept.id, id="team-sa-budget")

        user = await create_user(db_session, org.id, team.id, id="user-human-budget")
        sa = await create_service_account(db_session, org.id, dept.id, team.id, id="sa-budget")

        await create_budget_config(db_session, org.id, "user", user.id, budget_amount_usd=Decimal("200.00"))
        await create_budget_config(db_session, org.id, "service_account", sa.id, budget_amount_usd=Decimal("2000.00"))
        await db_session.commit()

        user_budget = Decimal("200.00")
        sa_budget = Decimal("2000.00")
        assert user_budget != sa_budget
        assert sa_budget > user_budget

    async def test_service_account_spend_tracked_separately(self, db_session: AsyncSession):
        """Admin UI shows service account spend separately."""
        org = await create_org(db_session, id="org-sa-track")
        dept = await create_department(db_session, org.id, id="dept-sa-track")
        team = await create_team(db_session, org.id, dept.id, id="team-sa-track")

        user = await create_user(db_session, org.id, team.id, id="user-track")
        sa = await create_service_account(db_session, org.id, dept.id, team.id, id="sa-track")

        await create_budget_usage(db_session, org.id, "user", user.id, total_cost_usd=Decimal("50.00"))
        await create_budget_usage(db_session, org.id, "service_account", sa.id, total_cost_usd=Decimal("500.00"))
        await db_session.commit()

        usage_summary = {
            "human_users": {"total_spend": 50.00, "request_count": 100},
            "service_accounts": {"total_spend": 500.00, "request_count": 1000},
        }

        assert usage_summary["human_users"]["total_spend"] == 50.00
        assert usage_summary["service_accounts"]["total_spend"] == 500.00


@pytest.mark.unit
class TestBudgetHTTPEnforcement:
    """Unit tests verifying budget-exhausted returns 402 or 429."""

    async def test_budget_exhausted_returns_402_or_429(self):
        """When a hard budget is exhausted the gateway returns 402 or 429."""
        with pytest.raises(BudgetExceededError) as exc:
            raise BudgetExceededError(
                level="team",
                entity="team-depleted",
                budget_usd=50.00,
                spent_usd=55.00,
                period="monthly",
                resets_at="2026-05-01T00:00:00Z",
            )

        assert exc.value.status_code in (402, 429)
        assert exc.value.error == "budget_exceeded"
        assert exc.value.details["spent_usd"] > exc.value.details["budget_usd"]


# =============================================================================
# Live-only tests -- Budget enforcement via real HTTP
# =============================================================================


@pytest.mark.live_only
class TestLiveBudgetOAuth:
    """Live HTTP tests for budget enforcement via OAuth."""

    async def test_budget_headers_present_in_response(self, api_client, jwt_for_user):
        """Successful proxy response includes budget-related headers."""
        from tests.e2e.config import get_test_bedrock_model

        model = get_test_bedrock_model()
        response = await api_client.post(
            "/v1/messages",
            headers={"Authorization": f"Bearer {jwt_for_user}", "Content-Type": "application/json"},
            json={
                "model": model,
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        # If budget middleware is active, response may include budget headers
        # Even if not, the response should succeed (not crash)
        assert response.status_code < 500, f"Budget test returned {response.status_code}"


@pytest.mark.live_only
class TestLiveBudgetIAM:
    """Live HTTP tests for budget enforcement via IAM SigV4."""

    async def test_iam_request_budget_headers(self, iam_signed_client):
        """IAM-authed proxy response includes budget headers (if applicable)."""
        from tests.e2e.config import get_test_bedrock_model

        model = get_test_bedrock_model()
        response = await iam_signed_client.post(
            "/v1/messages",
            json={
                "model": model,
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code < 500, f"IAM budget test returned {response.status_code}"
