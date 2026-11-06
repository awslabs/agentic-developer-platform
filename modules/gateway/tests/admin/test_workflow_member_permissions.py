"""Workflow membership grants do not confer administrative or agent authority."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.admin.exceptions import AccessDeniedError
from src.agentauth.human_control import authorize_human_session
from src.auth.workspaces import workflow_permissions
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.schemas.auth import TokenContext


@pytest.fixture
async def member(db_session):
    db_session.add(Organization(id="workspace", name="Example"))
    await db_session.flush()
    user = User(id="human", cognito_sub="login", org_id="workspace", team_id="", email="member@example.test")
    db_session.add(user)
    await db_session.flush()
    db_session.add(TenantMembership(user_id=user.id, tenant_id="workspace", role="member", is_active=False))
    await db_session.flush()
    return TokenContext(
        user_id="login", org_id="workspace", team_id="", department_id="", account_type="human", expires_at=datetime.now(UTC) + timedelta(hours=1)
    )


@pytest.mark.parametrize("role", ["member", "viewer", "dept_admin", "org_admin", "unknown-role"])
async def test_every_member_can_create_approve_and_run(db_session, member, role):
    membership = await db_session.scalar(select(TenantMembership))
    membership.role = role
    await db_session.flush()
    access = AccessControl(db_session)
    for permission in [Permission.PLAN_DRAFT, Permission.PLAN_APPROVE]:
        assert await access.check_permission(member, permission, target_org_id="workspace")
        assert await access.check_permission_for_discovery(member, permission, target_org_id="workspace") is True
    # A workspace selected in a different browser does not revoke this session.
    assert (await authorize_human_session(member, db_session)).user_id == "human"
    assert (await workflow_permissions(member, db_session))["permissions"] == ["plan:draft", "plan:approve"]


async def test_workflow_rights_do_not_grant_admin_rights(db_session, member):
    access = AccessControl(db_session)
    for permission in [Permission.USER_MANAGE, Permission.ORG_UPDATE, Permission.BUDGET_UPDATE, Permission.AGENT_REGISTER]:
        with pytest.raises(AccessDeniedError):
            await access.check_permission(member, permission)


@pytest.mark.parametrize(
    "changes",
    [
        {"org_id": "other"},
        {"org_id": ""},
        {"user_id": "unknown"},
        {"account_type": "service"},
        {"auth_source": "iam"},
        {"expires_at": datetime(2000, 1, 1, tzinfo=UTC)},
        {"user_id": "unknown", "is_admin": True},
    ],
)
async def test_invalid_caller_cannot_approve_even_with_admin_claim(db_session, member, changes):
    caller = member.model_copy(update=changes)
    access = AccessControl(db_session)
    with pytest.raises(AccessDeniedError):
        await access.check_permission(caller, Permission.PLAN_APPROVE)
    assert await access.check_permission_for_discovery(caller, Permission.PLAN_APPROVE) is False
    assert (await workflow_permissions(caller, db_session))["permissions"] == []


async def test_scope_and_live_revocation_override_cached_role(db_session, member):
    access = AccessControl(db_session)
    await access.get_user_role(member)
    with pytest.raises(AccessDeniedError):
        await access.check_permission(member, Permission.PLAN_APPROVE, target_org_id="other")
    assert await access.check_permission(member, Permission.PLAN_APPROVE)
    membership = await db_session.scalar(select(TenantMembership))
    membership.revoked_at = datetime.now(UTC)
    await db_session.flush()
    for permission in [Permission.PLAN_DRAFT, Permission.PLAN_APPROVE]:
        with pytest.raises(AccessDeniedError):
            await access.check_permission(member, permission)


@pytest.mark.parametrize("changes", [{"user_kind": "service"}, {"is_shadow": True}])
async def test_database_identity_must_also_be_human(db_session, member, changes):
    user = await db_session.get(User, "human")
    for field, value in changes.items():
        setattr(user, field, value)
    await db_session.flush()
    with pytest.raises(AccessDeniedError):
        await AccessControl(db_session).check_permission(member, Permission.PLAN_APPROVE)


async def test_membership_outage_denies_writes_but_discovery_reports_unavailable(db_session, member, monkeypatch):
    monkeypatch.setattr("src.shared.identity.workspaces.memberships_for_login", AsyncMock(side_effect=OSError("unavailable")))
    access = AccessControl(db_session)
    with pytest.raises(AccessDeniedError):
        await access.check_permission(member, Permission.PLAN_APPROVE)
    assert await access.check_permission_for_discovery(member, Permission.PLAN_APPROVE) is None
    with pytest.raises(HTTPException) as error:
        await workflow_permissions(member, db_session)
    assert error.value.status_code == 503


async def test_service_can_still_draft_but_cannot_approve(db_session, member):
    service = member.model_copy(update={"user_id": "agent", "account_type": "service", "auth_source": "iam"})
    access = AccessControl(db_session)
    assert await access.check_permission(service, Permission.PLAN_DRAFT)
    with pytest.raises(AccessDeniedError):
        await access.check_permission(service, Permission.PLAN_APPROVE)
