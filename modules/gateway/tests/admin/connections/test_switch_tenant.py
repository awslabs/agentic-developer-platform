"""Tests for switch-tenant endpoint — Issue #3071.

Verifies:
- Happy path: caller with 2 memberships can switch active tenant
- 403 when caller has no membership for target tenant
- cognito_sub ≠ users.id fixture (access-token-shaped — #3021/#3027 bug class)
- Post-switch get_connections returns is_active_tenant=true for the new tenant
- No-op when target is already active
- Exactly one active membership after switch (DB constraint respected)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import ChannelTenantMap
from src.shared.schemas.auth import TokenContext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def db_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_org(org_id: str, name: str) -> Organization:
    return Organization(
        id=org_id,
        name=name,
        aws_accounts=[],
        role_mappings={},
        settings={},
        github_installation_ids=[],
        cognito_client_ids=[],
    )


def _make_user(user_id: str, org_id: str, cognito_sub: str) -> User:
    return User(
        id=user_id,
        team_id="team-1",
        email=f"{user_id}@test.local",
        cognito_sub=cognito_sub,
        org_id=org_id,
    )


def _make_membership(user_id: str, tenant_id: str, is_active: bool = False) -> TenantMembership:
    return TenantMembership(
        user_id=user_id,
        tenant_id=tenant_id,
        role="member",
        is_active=is_active,
    )


def _make_connection(org_id: str, scope_id: str, installation_id: int, login: str) -> ChannelTenantMap:
    return ChannelTenantMap(
        provider="github",
        provider_scope_id=scope_id,
        org_id=org_id,
        install_metadata={
            "installation_id": installation_id,
            "account_login": login,
            "account_type": "Organization",
            "repository_selection": "all",
            "repository_count": 3,
        },
    )


def _make_token_context(cognito_sub: str, org_id: str) -> TokenContext:
    return TokenContext(
        user_id=cognito_sub,
        org_id=org_id,
        team_id="team-1",
        department_id="dept-1",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


# ---------------------------------------------------------------------------
# Tests: switch_tenant endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_tenant_happy_path(db_session: AsyncSession):
    """Caller with 2 memberships can switch active tenant. Exactly one active row after."""
    from src.admin.connections.routes import switch_tenant
    from src.admin.connections.schemas import SwitchTenantRequest

    cognito_sub = "cognito-sub-user1"
    pg_user_id = "pg-uuid-user1"

    # Setup: 2 orgs, 1 user, 2 memberships (tenant-a active)
    db_session.add_all(
        [
            _make_org("tenant-a", "Alpha Corp"),
            _make_org("tenant-b", "Beta Inc"),
        ]
    )
    db_session.add(_make_user(pg_user_id, "tenant-a", cognito_sub))
    db_session.add_all(
        [
            _make_membership(pg_user_id, "tenant-a", is_active=True),
            _make_membership(pg_user_id, "tenant-b", is_active=False),
        ]
    )
    await db_session.commit()

    # Execute: switch to tenant-b
    token = _make_token_context(cognito_sub, "tenant-a")
    body = SwitchTenantRequest(tenant_id="tenant-b")

    resp = await switch_tenant(body=body, current_user=token, db=db_session)

    assert resp.active_tenant_id == "tenant-b"

    # Verify: exactly one active membership, and it's tenant-b
    result = await db_session.execute(select(TenantMembership).where(TenantMembership.user_id == pg_user_id))
    memberships = result.scalars().all()
    active = [m for m in memberships if m.is_active]
    assert len(active) == 1
    assert active[0].tenant_id == "tenant-b"


@pytest.mark.asyncio
async def test_switch_tenant_403_no_membership(db_session: AsyncSession):
    """403 when caller has no membership row for target tenant."""
    from fastapi import HTTPException

    from src.admin.connections.routes import switch_tenant
    from src.admin.connections.schemas import SwitchTenantRequest

    cognito_sub = "cognito-sub-user1"
    pg_user_id = "pg-uuid-user1"

    # Setup: user has membership only in tenant-a
    db_session.add_all(
        [
            _make_org("tenant-a", "Alpha Corp"),
            _make_org("tenant-x", "Secret Corp"),
        ]
    )
    db_session.add(_make_user(pg_user_id, "tenant-a", cognito_sub))
    db_session.add(_make_membership(pg_user_id, "tenant-a", is_active=True))
    await db_session.commit()

    # Try to switch to tenant-x (no membership)
    token = _make_token_context(cognito_sub, "tenant-a")
    body = SwitchTenantRequest(tenant_id="tenant-x")

    with pytest.raises(HTTPException) as exc_info:
        await switch_tenant(body=body, current_user=token, db=db_session)

    assert exc_info.value.status_code == 403
    assert "No membership" in exc_info.value.detail


@pytest.mark.asyncio
async def test_switch_tenant_cognito_sub_not_users_id(db_session: AsyncSession):
    """cognito_sub ≠ users.id: endpoint correctly resolves via cognito_sub lookup."""
    from src.admin.connections.routes import switch_tenant
    from src.admin.connections.schemas import SwitchTenantRequest

    # cognito_sub and pg user id are deliberately different values
    cognito_sub = "cognito-sub-abc123"
    pg_user_id = "pg-uuid-xyz789"

    db_session.add_all(
        [
            _make_org("tenant-a", "Alpha Corp"),
            _make_org("tenant-b", "Beta Inc"),
        ]
    )
    db_session.add(_make_user(pg_user_id, "tenant-a", cognito_sub))
    db_session.add_all(
        [
            _make_membership(pg_user_id, "tenant-a", is_active=True),
            _make_membership(pg_user_id, "tenant-b", is_active=False),
        ]
    )
    await db_session.commit()

    # Token carries cognito_sub as user_id (not the pg UUID)
    token = _make_token_context(cognito_sub, "tenant-a")
    body = SwitchTenantRequest(tenant_id="tenant-b")

    resp = await switch_tenant(body=body, current_user=token, db=db_session)

    assert resp.active_tenant_id == "tenant-b"

    # Verify switch happened correctly despite cognito_sub ≠ users.id
    result = await db_session.execute(
        select(TenantMembership).where(
            TenantMembership.user_id == pg_user_id,
            TenantMembership.is_active == True,  # noqa: E712
        )
    )
    active = result.scalars().all()
    assert len(active) == 1
    assert active[0].tenant_id == "tenant-b"


@pytest.mark.asyncio
async def test_switch_tenant_noop_when_already_active(db_session: AsyncSession):
    """No-op when target is already the active tenant — returns immediately."""
    from src.admin.connections.routes import switch_tenant
    from src.admin.connections.schemas import SwitchTenantRequest

    cognito_sub = "cognito-sub-user1"
    pg_user_id = "pg-uuid-user1"

    db_session.add_all(
        [
            _make_org("tenant-a", "Alpha Corp"),
            _make_org("tenant-b", "Beta Inc"),
        ]
    )
    db_session.add(_make_user(pg_user_id, "tenant-a", cognito_sub))
    db_session.add_all(
        [
            _make_membership(pg_user_id, "tenant-a", is_active=True),
            _make_membership(pg_user_id, "tenant-b", is_active=False),
        ]
    )
    await db_session.commit()

    # Switch to already-active tenant-a
    token = _make_token_context(cognito_sub, "tenant-a")
    body = SwitchTenantRequest(tenant_id="tenant-a")

    resp = await switch_tenant(body=body, current_user=token, db=db_session)

    assert resp.active_tenant_id == "tenant-a"

    # Verify nothing changed — tenant-a still active, tenant-b still inactive
    result = await db_session.execute(select(TenantMembership).where(TenantMembership.user_id == pg_user_id))
    memberships = result.scalars().all()
    active = [m for m in memberships if m.is_active]
    assert len(active) == 1
    assert active[0].tenant_id == "tenant-a"


@pytest.mark.asyncio
async def test_switch_tenant_403_user_not_found(db_session: AsyncSession):
    """403 when cognito_sub doesn't resolve to any user row."""
    from fastapi import HTTPException

    from src.admin.connections.routes import switch_tenant
    from src.admin.connections.schemas import SwitchTenantRequest

    db_session.add(_make_org("tenant-a", "Alpha Corp"))
    await db_session.commit()

    # Token with a cognito_sub that doesn't exist in users table
    token = _make_token_context("nonexistent-cognito-sub", "tenant-a")
    body = SwitchTenantRequest(tenant_id="tenant-a")

    with pytest.raises(HTTPException) as exc_info:
        await switch_tenant(body=body, current_user=token, db=db_session)

    assert exc_info.value.status_code == 403
    assert "User not found" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Tests: post-switch get_connections reflects new active tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_switch_get_connections_reflects_new_active(db_session: AsyncSession):
    """After switch, get_connections returns is_active_tenant=true for the NEW tenant
    without token refresh (server-side read of DB state — Issue #3071 design).
    """
    from src.admin.connections.routes import get_connections, switch_tenant
    from src.admin.connections.schemas import SwitchTenantRequest

    cognito_sub = "cognito-sub-user1"
    pg_user_id = "pg-uuid-user1"

    db_session.add_all(
        [
            _make_org("tenant-a", "Alpha Corp"),
            _make_org("tenant-b", "Beta Inc"),
        ]
    )
    db_session.add(_make_user(pg_user_id, "tenant-a", cognito_sub))
    db_session.add_all(
        [
            _make_membership(pg_user_id, "tenant-a", is_active=True),
            _make_membership(pg_user_id, "tenant-b", is_active=False),
        ]
    )
    db_session.add_all(
        [
            _make_connection("tenant-a", "111", 1001, "alpha-gh"),
            _make_connection("tenant-b", "222", 1002, "beta-gh"),
        ]
    )
    await db_session.commit()

    # Token still says org_id="tenant-a" (token hasn't refreshed)
    token = _make_token_context(cognito_sub, "tenant-a")

    # Switch to tenant-b
    body = SwitchTenantRequest(tenant_id="tenant-b")
    await switch_tenant(body=body, current_user=token, db=db_session)

    # Call get_connections with the SAME token (still says tenant-a)
    # Server should prefer DB is_active (now tenant-b) over token claim
    resp = await get_connections(current_user=token, db=db_session)

    assert len(resp.connections) == 2
    by_login = {c.account_login: c for c in resp.connections}

    # tenant-b should now be the active tenant (even though token says tenant-a)
    assert by_login["beta-gh"].is_active_tenant is True
    assert by_login["alpha-gh"].is_active_tenant is False
