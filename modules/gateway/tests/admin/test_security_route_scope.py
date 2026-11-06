"""Security findings #8785: real membership and entity boundaries on legacy reads."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.admin.access_control import AccessControl
from src.admin.config import AdminRole, Permission
from src.admin.exceptions import AccessDeniedError, InvalidScopeError, ResourceNotFoundError
from src.admin.routes import get_budget_config, get_budget_status, list_agents, list_budgets
from src.admin.service import AdminService
from src.budget.service import BudgetService
from src.shared.models.budget import BudgetConfig
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, TeamMembership, User
from src.shared.schemas.auth import TokenContext


@pytest.fixture
async def scoped_caller(db_session):
    db_session.add_all([Organization(id=org, name=org) for org in ["one", "two"]])
    for dept, org in [("a", "one"), ("b", "one"), ("foreign", "two")]:
        db_session.add(Department(id=dept, org_id=org, name=dept))
        db_session.add(Team(id=f"team-{dept}", org_id=org, department_id=dept, name=dept))
    db_session.add(User(id="caller", cognito_sub="login", org_id="one", team_id="team-b", email="caller@example.test"))
    await db_session.flush()
    db_session.add(TenantMembership(user_id="caller", tenant_id="one", role="dept_admin"))
    # Authoritative primary contradicts both the legacy pointer and token hints.
    db_session.add(TeamMembership(user_id="caller", org_id="one", team_id="team-a", is_primary=True))
    for kind, identity, org in [
        ("department", "a", "one"),
        ("department", "b", "one"),
        ("team", "team-a", "one"),
        ("team", "team-b", "one"),
        ("department", "foreign", "two"),
        ("org", "one", "one"),
        ("user", "login", "one"),
        ("root_user", "caller", "one"),
    ]:
        db_session.add(BudgetConfig(org_id=org, entity_type=kind, entity_id=identity, period_type="daily", budget_amount_usd=Decimal("7")))
    await db_session.commit()
    return TokenContext(
        user_id="login", org_id="one", team_id="team-b", department_id="b", account_type="human", expires_at=datetime.now(UTC) + timedelta(hours=1)
    )


def admin_service(db):
    return AdminService(db, budget_service=BudgetService(db))


@pytest.mark.parametrize("read", [get_budget_config, get_budget_status])
@pytest.mark.parametrize("kind,key", [("department", "b"), ("team", "team-b"), ("org", "one")])
async def test_legacy_budget_reads_deny_other_department_and_org_wide(db_session, scoped_caller, read, kind, key):
    with pytest.raises(AccessDeniedError):
        await read("one", kind, key, admin_service(db_session), AccessControl(db_session), scoped_caller)


@pytest.mark.parametrize("read", [get_budget_config, get_budget_status])
async def test_legacy_budget_reads_validate_entity_ownership(db_session, scoped_caller, read):
    with pytest.raises(ResourceNotFoundError):
        await read("one", "department", "foreign", admin_service(db_session), AccessControl(db_session), scoped_caller)
    with pytest.raises(InvalidScopeError):
        await read("two", "department", "foreign", admin_service(db_session), AccessControl(db_session), scoped_caller)


@pytest.mark.parametrize("read", [get_budget_config, get_budget_status])
@pytest.mark.parametrize(
    "kind,key,canonical", [("department", "a", "a"), ("team", "team-a", "team-a"), ("user", "caller", "login"), ("root_user", "login", "caller")]
)
async def test_department_admin_reads_canonical_targets_in_live_primary_department(db_session, scoped_caller, read, kind, key, canonical):
    result = await read("one", kind, key, admin_service(db_session), AccessControl(db_session), scoped_caller)
    if read is get_budget_config:
        assert result.entity_id == canonical
    assert result.budget_amount_usd == Decimal("7")


async def test_department_scope_is_available_to_discovery_and_list(db_session, scoped_caller):
    access = AccessControl(db_session)
    assert await access.get_user_role_for_discovery(scoped_caller) == (AdminRole.DEPT_ADMIN, "one", "a")
    result = await list_budgets("one", admin_service(db_session), access, scoped_caller, None)
    assert {(row.entity_type, row.entity_id) for row in result.items} == {
        ("department", "a"),
        ("team", "team-a"),
        ("user", "login"),
        ("root_user", "caller"),
    }
    assert all(row.entity_id not in {"one", "b", "team-b", "foreign"} for row in result.items)


async def test_department_without_primary_fails_closed(db_session, scoped_caller):
    from sqlalchemy import update

    await db_session.execute(update(TeamMembership).values(is_primary=False))
    await db_session.commit()
    access = AccessControl(db_session)
    with pytest.raises(AccessDeniedError):
        await get_budget_config("one", "department", "a", admin_service(db_session), access, scoped_caller)
    with pytest.raises(InvalidScopeError):
        await access.check_permission(scoped_caller, Permission.BUDGET_READ, target_org_id="one", target_dept_id="a")


@pytest.mark.parametrize("explicit", [False, True])
async def test_agent_inventory_requires_permission_with_implicit_or_explicit_org(db_session, scoped_caller, explicit):
    from sqlalchemy import update

    await db_session.execute(update(TenantMembership).values(role="member"))
    await db_session.commit()
    inventory = AsyncMock()
    with pytest.raises(AccessDeniedError):
        await list_agents(inventory, AccessControl(db_session), scoped_caller, "one" if explicit else None)
    inventory.list_agents.assert_not_awaited()


async def test_org_admin_can_read_default_inventory_but_empty_scope_is_denied(db_session, scoped_caller):
    from sqlalchemy import update

    await db_session.execute(update(TenantMembership).values(role="admin"))
    await db_session.commit()
    inventory = AsyncMock()
    await list_agents(inventory, AccessControl(db_session), scoped_caller)
    inventory.list_agents.assert_awaited_once_with("one", 1, 50)
    with pytest.raises(AccessDeniedError):
        await list_agents(inventory, AccessControl(db_session), scoped_caller.model_copy(update={"org_id": "", "is_admin": True}))
    assert inventory.list_agents.await_count == 1


async def test_department_list_cannot_use_stale_target_team_pointer(db_session, scoped_caller):
    # A different person's legacy pointer says A, but their primary membership
    # says B. A's admin must not learn that person's budget through list APIs.
    db_session.add(User(id="other", cognito_sub="other-login", org_id="one", team_id="team-a", email="other@example.test"))
    await db_session.flush()
    db_session.add(TeamMembership(user_id="other", org_id="one", team_id="team-b", is_primary=True))
    db_session.add(BudgetConfig(org_id="one", entity_type="user", entity_id="other-login", period_type="daily", budget_amount_usd=Decimal("91")))
    await db_session.commit()
    service = admin_service(db_session)
    result = await list_budgets("one", service, AccessControl(db_session), scoped_caller, None)
    assert all(row.entity_id != "other-login" for row in result.items)
    with pytest.raises(AccessDeniedError):
        await get_budget_status("one", "user", "other", service, AccessControl(db_session), scoped_caller)


async def test_department_lookup_failure_cannot_activate_legacy_org_admin_fallback(db_session, scoped_caller, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "src.admin.access_control.get_admin_config", lambda: SimpleNamespace(rbac_least_privilege_default=False, rbac_role_cache_ttl_seconds=0)
    )
    access = AccessControl(db_session)
    monkeypatch.setattr(access, "_resolve_department", AsyncMock(side_effect=OSError("unavailable")))
    assert await access.get_user_role(scoped_caller) == (AdminRole.DEPT_ADMIN, "one", None)
    with pytest.raises(AccessDeniedError):
        await get_budget_status("one", "department", "b", admin_service(db_session), access, scoped_caller)
