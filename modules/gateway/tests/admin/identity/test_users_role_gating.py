"""Issue #4006: the identity create-user path must gate the role and write a membership.

``UserCreateRequest.role`` is a free-form ``str`` defaulting to ``"member"``
(``identity/schemas.py``), and this route — unlike the equivalent in
``admin/routes.py`` — applied no role-assignment ceiling, so an org_admin caller
could mint a ``platform_admin`` users row. It also wrote no ``tenant_memberships``
row, so an admin-level create produced a "no-row" admin that re-dirtied the #3987
audit after any backfill.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.exceptions import InvalidScopeError
from src.admin.identity.schemas import UserCreateRequest
from src.admin.identity.users_service import UsersService
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.schemas.auth import TokenContext

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_cognito_sync():
    """Stub the Cognito invite so create_user makes no AWS calls."""
    mock = AsyncMock()
    mock.create_user_and_invite = AsyncMock(return_value={"Username": "stub"})
    mock.delete_user = AsyncMock(return_value=True)
    return mock


@pytest.fixture
async def seeded_org(db_session: AsyncSession):
    org = Organization(
        id="gate-org",
        name="Gate Org",
        aws_accounts=[],
        role_mappings={},
        settings={},
        github_installation_ids=[],
        cognito_client_ids=[],
    )
    db_session.add(org)
    db_session.add(Department(id="gate-org-dept-default", org_id="gate-org", name="Default"))
    db_session.add(Team(id="gate-org-team-default", org_id="gate-org", department_id="gate-org-dept-default", name="Default"))
    await db_session.commit()
    return org


# ---------------------------------------------------------------------------
# Role-assignment ceiling (the gate the route now applies)
# ---------------------------------------------------------------------------


async def test_org_admin_caller_cannot_create_platform_admin(db_session: AsyncSession, org_admin_context: TokenContext, org_admin_membership):
    """The escalation vector: a non-platform caller may not mint a platform role.

    Issue #3987 PR 2: takes ``org_admin_membership`` so the caller is a genuine
    org admin. Without it the caller resolves to MEMBER (rank 0, assigns nothing),
    so the raise would no longer prove anything about the org_admin ceiling.
    """
    with pytest.raises(InvalidScopeError):
        await AccessControl(db_session).require_assignable_role(org_admin_context, "platform_admin", target_org_id="gate-org")


async def test_platform_admin_caller_can_create_org_admin(db_session: AsyncSession, platform_admin_context: TokenContext):
    """A legitimate platform admin can still create an admin-level user."""
    await AccessControl(db_session).require_assignable_role(platform_admin_context, "org_admin", target_org_id="gate-org")


# ---------------------------------------------------------------------------
# Membership write on admin-level creates
# ---------------------------------------------------------------------------


async def test_admin_level_create_writes_org_admin_membership(db_session: AsyncSession, seeded_org, mock_cognito_sync):
    """An admin-level create writes the tenant_memberships row that now carries
    that authority — otherwise the new admin is a no-row principal."""
    svc = UsersService(db_session, cognito_sync=mock_cognito_sync)
    result = await svc.create_user("gate-org", UserCreateRequest(email="newadmin@test.com", name="New Admin", role="org_admin", send_invite=False))

    rows = list((await db_session.execute(select(TenantMembership).where(TenantMembership.user_id == result.id))).scalars().all())
    assert len(rows) == 1
    assert rows[0].tenant_id == "gate-org"
    assert rows[0].role == "org_admin"
    assert rows[0].is_active is True
    assert rows[0].joined_via == "admin_create"


async def test_platform_admin_role_is_stored_as_org_admin(db_session: AsyncSession, seeded_org, mock_cognito_sync):
    """A tenant-scoped row must never carry platform authority (#3981): the
    users.role stays platform_admin, the membership collapses to org_admin."""
    svc = UsersService(db_session, cognito_sync=mock_cognito_sync)
    result = await svc.create_user("gate-org", UserCreateRequest(email="plat@test.com", role="platform_admin", send_invite=False))

    user = await db_session.get(User, result.id)
    assert user.role == "platform_admin"

    rows = list((await db_session.execute(select(TenantMembership).where(TenantMembership.user_id == result.id))).scalars().all())
    assert len(rows) == 1
    assert rows[0].role == "org_admin"


async def test_member_level_create_writes_no_membership(db_session: AsyncSession, seeded_org, mock_cognito_sync):
    """Unchanged behaviour for non-admin creates — no membership, no elevation."""
    svc = UsersService(db_session, cognito_sync=mock_cognito_sync)
    result = await svc.create_user("gate-org", UserCreateRequest(email="plain@test.com", role="member", send_invite=False))

    rows = list((await db_session.execute(select(TenantMembership).where(TenantMembership.user_id == result.id))).scalars().all())
    assert rows == []
