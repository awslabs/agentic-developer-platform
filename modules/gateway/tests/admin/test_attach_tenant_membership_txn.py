"""Issue #4006: _attach_user_to_existing_tenant writes the membership atomically.

Before this fix the ``org_admin`` ``users`` row committed inside
``_attach_user_to_existing_tenant`` while the membership was written by the caller
(``submit_access_request``) in a *separate* transaction. A crash between the two
commits left a no-row admin — an admin whose authority depended entirely on the
legacy ORG_ADMIN fallback that #3987 PR 2 removes.

These tests call the attach function directly (no caller, so no second
transaction) and assert the membership is already there.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.onboarding.handler import _attach_user_to_existing_tenant
from src.shared.models.base import new_uuid
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, User

pytestmark = pytest.mark.asyncio

V2_ON = {"USER_IDENTITY_INDEX_V2_WRITE": "true"}


def _mock_github_client(role: str):
    """Mock GitHubAppClient whose /orgs/{org}/memberships/{user} returns `role`."""
    client = MagicMock()
    client.check_org_membership = AsyncMock(return_value=True)
    client.get_installation_token = AsyncMock(return_value="fake-token")
    client.aclose = AsyncMock()
    client._http_client = MagicMock()
    client._http_client.get = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"role": role}))
    return client


async def _seed_org(db: AsyncSession, org_id: str = "existing-org") -> None:
    org = Organization(
        id=org_id,
        name=org_id,
        aws_accounts=[],
        role_mappings={},
        settings={},
        github_installation_ids=["12345"],
        cognito_client_ids=[],
        member_approval_policy="auto_approve_org_members",
    )
    db.add(org)
    dept = Department(id=new_uuid(), org_id=org_id, name="Default")
    db.add(dept)
    db.add(Team(id=new_uuid(), org_id=org_id, department_id=dept.id, name="Default"))
    await db.commit()


async def _attach(db: AsyncSession, *, github_role: str, login: str, sub: str, org_id: str = "existing-org"):
    client = _mock_github_client(github_role)
    with (
        patch("src.admin.connections.github_client.GitHubAppClient", return_value=client),
        patch("src.admin.connections.service._get_github_app_credentials", return_value=("app-id", "fake-pem")),
    ):
        return await _attach_user_to_existing_tenant(
            db=db,
            org_id=org_id,
            cognito_sub=sub,
            github_login=login,
            github_id="70007",
        )


@patch.dict(os.environ, V2_ON)
async def test_attach_org_admin_writes_membership_in_same_txn(db_session: AsyncSession):
    """A GitHub org admin attached to an existing tenant gets an org_admin
    membership written by the attach function itself, not by its caller."""
    await _seed_org(db_session)

    response = await _attach(db_session, github_role="admin", login="attachadmin", sub="cognito-sub-attach")
    assert response.status == "approved"

    user = await db_session.scalar(select(User).where(User.cognito_sub == "cognito-sub-attach"))
    assert user.role == "org_admin"

    rows = list((await db_session.execute(select(TenantMembership).where(TenantMembership.user_id == user.id))).scalars().all())
    assert len(rows) == 1
    assert rows[0].tenant_id == "existing-org"
    assert rows[0].role == "org_admin"
    assert rows[0].is_active is True
    assert rows[0].joined_via == "org_membership"


@patch.dict(os.environ, V2_ON)
async def test_attach_plain_member_keeps_member_role(db_session: AsyncSession):
    """A non-admin GitHub org member must NOT be elevated by the new write."""
    await _seed_org(db_session)

    response = await _attach(db_session, github_role="member", login="attachmember", sub="cognito-sub-member")
    assert response.status == "approved"

    user = await db_session.scalar(select(User).where(User.cognito_sub == "cognito-sub-member"))
    rows = list((await db_session.execute(select(TenantMembership).where(TenantMembership.user_id == user.id))).scalars().all())
    assert len(rows) == 1
    assert rows[0].role == "member"


@patch.dict(os.environ, V2_ON)
async def test_attach_pending_policy_writes_no_membership(db_session: AsyncSession):
    """When the org requires approval, no user and no membership are created."""
    await _seed_org(db_session, org_id="manual-org")
    org = await db_session.get(Organization, "manual-org")
    org.member_approval_policy = "manual_approval"
    await db_session.commit()

    response = await _attach(db_session, github_role="admin", login="waiting", sub="cognito-sub-wait", org_id="manual-org")
    assert response.status == "pending"

    rows = list((await db_session.execute(select(TenantMembership))).scalars().all())
    assert rows == []
