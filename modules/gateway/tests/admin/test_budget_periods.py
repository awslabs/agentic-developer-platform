"""Exact period/ledger writes preserve independent caps and recorded spend."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.admin.access_control import AccessControl
from src.admin.config import AdminRole, Permission
from src.admin.exceptions import AccessDeniedError, ResourceConflictError, ResourceNotFoundError
from src.admin.routes import _exact_budget_target
from src.admin.schemas import BudgetPeriodSetRequest
from src.admin.service import AdminService
from src.shared.models.budget import BudgetUsage
from src.shared.models.organization import Department, Team, User
from src.shared.schemas.auth import TokenContext


def request(amount="1.00", **kwargs):
    return BudgetPeriodSetRequest(budget_amount_usd=amount, enforcement_mode="hard", **kwargs)


async def test_periods_coexist_update_delete_never_reset_usage(db_session, sample_organizations):
    org = sample_organizations[0].id
    service = AdminService(db_session)
    replies = {p: await service.set_exact_budget(org, "org", org, p, request(expect_absent=True)) for p in ["daily", "weekly", "monthly"]}
    db_session.add(
        BudgetUsage(org_id=org, entity_type="org", entity_id=org, period_type="weekly", period_start=date.today(), total_cost_usd=Decimal("0.123456"))
    )
    await db_session.commit()
    daily = await service.set_exact_budget(org, "org", org, "daily", request("2.00", expected_revision=replies["daily"].updated_at))
    assert daily.budget_amount_usd == Decimal("2.00")
    assert (await service.exact_budget(org, "org", org, "monthly")).budget_amount_usd == Decimal("1.00")
    await service.delete_exact_budget(org, "org", org, "weekly", replies["weekly"].updated_at)
    assert await service.exact_budget(org, "org", org, "weekly") is None
    assert (await db_session.execute(select(BudgetUsage))).scalar_one().total_cost_usd == Decimal("0.123456")


async def test_stale_or_duplicate_create_cannot_overwrite_cap(db_session, sample_organizations):
    org = sample_organizations[0].id
    service = AdminService(db_session)
    initial = await service.set_exact_budget(org, "org", org, "daily", request(expect_absent=True))
    for change in [request("9.00", expect_absent=True), request("9.00", expected_revision=initial.updated_at - timedelta(seconds=1))]:
        with pytest.raises(ResourceConflictError):
            await service.set_exact_budget(org, "org", org, "daily", change)
    with pytest.raises(ResourceConflictError):
        await service.delete_exact_budget(org, "org", org, "daily", initial.updated_at - timedelta(seconds=1))
    assert (await service.exact_budget(org, "org", org, "daily")).budget_amount_usd == Decimal("1.00")


async def identity_rows(db, org):
    db.add(Department(id="department-a", org_id=org, name="a"))
    db.add(Team(id="team-a", org_id=org, department_id="department-a", name="team"))
    db.add(User(id="canonical-human", org_id=org, team_id="team-a", email="human@example.invalid", cognito_sub="login-subject"))
    await db.commit()


async def test_personal_and_cloud_canonical_ids_and_department(db_session, sample_organizations):
    org = sample_organizations[0].id
    await identity_rows(db_session, org)
    service = AdminService(db_session)
    assert await service.resolve_budget_target(org, "user", "canonical-human") == ("login-subject", "department-a")
    assert await service.resolve_budget_target(org, "root_user", "login-subject") == ("canonical-human", "department-a")
    with pytest.raises(ResourceNotFoundError):
        await service.resolve_budget_target(sample_organizations[1].id, "team", "team-a")


async def test_department_list_cannot_expose_org_or_other_department(db_session, sample_organizations):
    org = sample_organizations[0].id
    await identity_rows(db_session, org)
    service = AdminService(db_session)
    for kind, key in [
        ("org", org),
        ("department", "department-a"),
        ("department", "department-other"),
        ("team", "team-a"),
        ("user", "login-subject"),
        ("root_user", "canonical-human"),
    ]:
        await service.set_exact_budget(org, kind, key, "daily", request(expect_absent=True))
    result = await service.get_budgets_list(org, department_id="department-a")
    assert {(r.entity_type, r.entity_id) for r in result.items} == {
        ("department", "department-a"),
        ("team", "team-a"),
        ("user", "login-subject"),
        ("root_user", "canonical-human"),
    }
    assert result.total == 4


async def test_department_admin_no_org_target_even_without_optional_scope_predicate(db_session, sample_organizations, monkeypatch):
    org = sample_organizations[0].id
    service = AdminService(db_session)
    access = AccessControl(db_session)
    monkeypatch.setattr(access, "get_user_role", AsyncMock(return_value=(AdminRole.DEPT_ADMIN, org, "department-a")))
    caller = TokenContext(
        user_id="human",
        org_id=org,
        team_id="team-a",
        department_id="department-a",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    with pytest.raises(AccessDeniedError):
        await _exact_budget_target(org, "org", org, service, access, caller, Permission.BUDGET_UPDATE)
