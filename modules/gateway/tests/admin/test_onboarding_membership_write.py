"""Issue #4006: the org-creator write paths must populate tenant_memberships.

Migration 021 (#2961) made ``tenant_memberships.role`` the store for org-level
role and #3987 PR 1 (#3998) made the read side trust it, but the write paths that
mint an admin-level ``users`` row never wrote one. Every newly-onboarded org admin
was therefore a "no-row" principal holding authority only via the legacy ORG_ADMIN
fallback — and #3987 PR 2 removes that fallback.

These tests pin the write side: the membership row exists, carries ``org_admin``
(not ``member``), lands in the same transaction as the ``users`` row, and a
re-approve upserts rather than duplicating.

Postgres-vs-SQLite caveat: this suite is SQLite (``tests/conftest.py:28``), which
builds neither migration 021's PostgreSQL-only partial unique index
``uq_tenant_memberships_one_active`` nor (via ``create_all``) any partial index.
The tests therefore prove the *portable* upsert semantics — one row per
(user, tenant), ``is_active`` only when no other tenant is active — which is what
keeps the production constraints satisfied. They cannot themselves prove the
constraint is honoured; that requires Postgres.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.onboarding.approval import approve_request
from src.shared.models.base import new_uuid
from src.shared.models.onboarding import TenantAccessRequest, TenantMembership
from src.shared.models.organization import Department, Organization, Team, User

pytestmark = pytest.mark.asyncio

V2_ON = {"USER_IDENTITY_INDEX_V2_WRITE": "true"}


def _pending_request(*, tenant_id: str = "acme-new", login: str = "acmeowner", sub: str = "cognito-sub-owner") -> TenantAccessRequest:
    return TenantAccessRequest(
        id=new_uuid(),
        cognito_sub=sub,
        provider="github",
        provider_user_id="90001",
        proposed_tenant_id=tenant_id,
        target_login=login,
        motivation="new workspace",
        status="pending",
    )


async def _memberships(db: AsyncSession, user_id: str) -> list[TenantMembership]:
    return list((await db.execute(select(TenantMembership).where(TenantMembership.user_id == user_id))).scalars().all())


# ---------------------------------------------------------------------------
# approve_request — the core fix
# ---------------------------------------------------------------------------


@patch.dict(os.environ, V2_ON)
async def test_approve_request_writes_active_org_admin_membership(db_session: AsyncSession):
    """The core fix: a fresh org approval writes one active org_admin membership."""
    request = _pending_request()
    db_session.add(request)
    await db_session.flush()

    tenant_id = await approve_request(db=db_session, request=request, admin_sub="admin-1")
    assert tenant_id == "acme-new"

    user = await db_session.scalar(select(User).where(User.cognito_sub == "cognito-sub-owner"))
    assert user is not None
    assert user.role == "org_admin"

    rows = await _memberships(db_session, user.id)
    assert len(rows) == 1
    assert rows[0].tenant_id == "acme-new"
    assert rows[0].role == "org_admin"
    assert rows[0].is_active is True
    assert rows[0].joined_via == "onboarding_approval"


@patch.dict(os.environ, V2_ON)
async def test_approve_request_membership_role_is_not_member(db_session: AsyncSession):
    """Guards the wrong-role failure mode: a 'member' row would BEAT the ORG_ADMIN
    fallback on the read side and lock the org creator out of their own org."""
    request = _pending_request()
    db_session.add(request)
    await db_session.flush()
    await approve_request(db=db_session, request=request, admin_sub="admin-1")

    user = await db_session.scalar(select(User).where(User.cognito_sub == "cognito-sub-owner"))
    rows = await _memberships(db_session, user.id)
    assert rows[0].role != "member"


@patch.dict(os.environ, V2_ON)
async def test_approve_request_membership_is_same_transaction_as_user(db_session: AsyncSession):
    """Durability: the membership must be visible in the same flush as the user,
    i.e. before the commit — not written by a later, separate transaction."""
    request = _pending_request()
    db_session.add(request)
    await db_session.flush()

    seen: dict[str, int] = {}
    real_commit = db_session.commit

    async def _spy_commit():
        # At commit time both rows must already be pending in this transaction.
        user_row = await db_session.scalar(select(User).where(User.cognito_sub == "cognito-sub-owner"))
        seen["user"] = 1 if user_row is not None else 0
        seen["memberships"] = len(await _memberships(db_session, user_row.id)) if user_row else 0
        await real_commit()

    with patch.object(db_session, "commit", _spy_commit):
        await approve_request(db=db_session, request=request, admin_sub="admin-1")

    assert seen["user"] == 1
    assert seen["memberships"] == 1


@patch.dict(os.environ, V2_ON)
async def test_reapprove_upserts_membership_no_duplicate(db_session: AsyncSession):
    """Idempotent re-approve: exactly one active org_admin row, never two."""
    first = _pending_request()
    db_session.add(first)
    await db_session.flush()
    await approve_request(db=db_session, request=first, admin_sub="admin-1")

    user = await db_session.scalar(select(User).where(User.cognito_sub == "cognito-sub-owner"))

    second = _pending_request()  # same tenant + sub → existing_org branch
    db_session.add(second)
    await db_session.flush()
    await approve_request(db=db_session, request=second, admin_sub="admin-1")

    rows = await _memberships(db_session, user.id)
    assert len(rows) == 1
    assert rows[0].role == "org_admin"
    assert rows[0].is_active is True


@patch.dict(os.environ, V2_ON)
async def test_reapprove_heals_preexisting_no_row_admin(db_session: AsyncSession):
    """The embark1 cohort: an admin onboarded before this fix has no membership
    row. Re-approving must create it (this is the self-heal path)."""
    org = Organization(
        id="legacy-org",
        name="legacy-org",
        aws_accounts=[],
        role_mappings={},
        settings={},
        github_installation_ids=[],
        cognito_client_ids=[],
    )
    db_session.add(org)
    dept = Department(id=new_uuid(), org_id="legacy-org", name="Default")
    db_session.add(dept)
    team = Team(id=new_uuid(), org_id="legacy-org", department_id=dept.id, name="Default")
    db_session.add(team)
    db_session.add(
        User(
            id="legacy-admin-1",
            org_id="legacy-org",
            team_id=team.id,
            email="legacy@github.onboard",
            name="legacyadmin",
            cognito_sub="cognito-sub-legacy",
            role="org_admin",
        )
    )
    await db_session.commit()
    assert await _memberships(db_session, "legacy-admin-1") == []

    request = _pending_request(tenant_id="legacy-org", login="legacyadmin", sub="cognito-sub-legacy")
    db_session.add(request)
    await db_session.flush()
    await approve_request(db=db_session, request=request, admin_sub="admin-1")

    rows = await _memberships(db_session, "legacy-admin-1")
    assert len(rows) == 1
    assert rows[0].tenant_id == "legacy-org"
    assert rows[0].role == "org_admin"
    assert rows[0].is_active is True


# ---------------------------------------------------------------------------
# bootstrap_first_admin — the deliberate users.role / membership.role split
# ---------------------------------------------------------------------------


@patch.dict(os.environ, V2_ON)
async def test_bootstrap_admin_membership_is_org_admin_not_platform_admin(db_session: AsyncSession):
    """bootstrap_first_admin sets users.role='platform_admin' AFTER approve_request
    returns. The membership must stay 'org_admin': a tenant-scoped row must never
    confer platform authority (#3981; config.py maps platform_admin -> ORG_ADMIN
    anyway, so nothing is lost)."""
    from src.admin.onboarding.bootstrap_admin import bootstrap_first_admin

    writer = MagicMock()
    writer.put_user_identity = AsyncMock(return_value=True)

    result = await bootstrap_first_admin(
        db_session,
        cognito_sub="cognito-sub-bootstrap",
        org_id="platform-admin",
        display_name="platform-admin",
        email="admin@example.com",
        identity_writer=writer,
    )
    assert result["status"] == "bootstrapped"

    user = await db_session.scalar(select(User).where(User.cognito_sub == "cognito-sub-bootstrap"))
    assert user.role == "platform_admin"

    rows = await _memberships(db_session, user.id)
    assert len(rows) == 1
    assert rows[0].role == "org_admin"
    assert rows[0].tenant_id == "platform-admin"
