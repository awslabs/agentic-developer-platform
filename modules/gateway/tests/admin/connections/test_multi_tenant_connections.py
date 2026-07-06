"""Tests for multi-tenant connections visibility — Issue #3018.

Verifies:
- Multi-tenant aggregation: user with memberships in multiple tenants sees all connections
- Leak guard: user without membership in tenant X cannot see X's connections
- Legacy fallback: user with no memberships gets single-org behavior
- adp-default personal scoping preserved per-tenant in multi-tenant mode
- Active-tenant flagging: is_active_tenant correctly identifies the caller's current tenant
- Route-level integration: cognito_sub → users.id resolution produces multi-tenant results
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import ChannelTenantMap

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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_tenant_aggregation(db_session: AsyncSession):
    """User with memberships in 3 tenants sees connections from all 3, each tagged."""
    from src.admin.connections.service import list_connections

    # Setup 3 orgs
    db_session.add_all(
        [
            _make_org("tenant-a", "Alpha Corp"),
            _make_org("tenant-b", "Beta Inc"),
            _make_org("tenant-c", "Gamma Ltd"),
        ]
    )
    # Setup connections in each
    db_session.add_all(
        [
            _make_connection("tenant-a", "111", 1001, "alpha-gh"),
            _make_connection("tenant-b", "222", 1002, "beta-gh"),
            _make_connection("tenant-c", "333", 1003, "gamma-gh"),
        ]
    )
    await db_session.commit()

    resp = await list_connections(
        caller_org_id="tenant-a",
        caller_user_id="user-1",
        db=db_session,
        member_tenant_ids=["tenant-a", "tenant-b", "tenant-c"],
    )

    assert len(resp.connections) == 3
    logins = {c.account_login for c in resp.connections}
    assert logins == {"alpha-gh", "beta-gh", "gamma-gh"}

    # Verify tagging
    for conn in resp.connections:
        assert conn.tenant_id is not None
        assert conn.tenant_name is not None

    # Active tenant flagging
    active_conns = [c for c in resp.connections if c.is_active_tenant]
    assert len(active_conns) == 1
    assert active_conns[0].tenant_id == "tenant-a"


@pytest.mark.asyncio
async def test_leak_guard_no_membership(db_session: AsyncSession):
    """User without membership in tenant X cannot see X's connections."""
    from src.admin.connections.service import list_connections

    # Setup 2 orgs
    db_session.add_all(
        [
            _make_org("tenant-a", "Alpha Corp"),
            _make_org("tenant-x", "Secret Corp"),
        ]
    )
    # Connections exist in both
    db_session.add_all(
        [
            _make_connection("tenant-a", "111", 1001, "alpha-gh"),
            _make_connection("tenant-x", "999", 9001, "secret-gh"),
        ]
    )
    await db_session.commit()

    # User is only a member of tenant-a (tenant-x NOT in member_tenant_ids)
    resp = await list_connections(
        caller_org_id="tenant-a",
        caller_user_id="user-1",
        db=db_session,
        member_tenant_ids=["tenant-a"],
    )

    assert len(resp.connections) == 1
    assert resp.connections[0].account_login == "alpha-gh"


@pytest.mark.asyncio
async def test_legacy_fallback_no_memberships(db_session: AsyncSession):
    """User with no memberships (legacy path) gets single-org behavior — no tenant tags."""
    from src.admin.connections.service import list_connections

    db_session.add(_make_org("tenant-a", "Alpha Corp"))
    db_session.add(_make_connection("tenant-a", "111", 1001, "alpha-gh"))
    await db_session.commit()

    # member_tenant_ids=None → legacy single-org
    resp = await list_connections(
        caller_org_id="tenant-a",
        caller_user_id="user-1",
        db=db_session,
        member_tenant_ids=None,
    )

    assert len(resp.connections) == 1
    conn = resp.connections[0]
    assert conn.account_login == "alpha-gh"
    # Legacy mode: no tenant tagging
    assert conn.tenant_id is None
    assert conn.tenant_name is None
    assert conn.is_active_tenant is None


@pytest.mark.asyncio
async def test_adp_default_personal_scoping_multi_tenant(db_session: AsyncSession):
    """adp-default personal installs scoped to caller in multi-tenant mode."""
    from src.admin.connections.service import list_connections

    adp_default_id = "adp-default"

    db_session.add_all(
        [
            _make_org("tenant-a", "Alpha Corp"),
            _make_org(adp_default_id, "ADP Default"),
        ]
    )
    # Two personal connections in adp-default — different users
    db_session.add(
        ChannelTenantMap(
            provider="github",
            provider_scope_id="personal:444:user-1",
            org_id=adp_default_id,
            install_metadata={
                "installation_id": 2001,
                "account_login": "user1-gh",
                "account_type": "User",
                "repository_selection": "selected",
                "repository_count": 1,
            },
        )
    )
    db_session.add(
        ChannelTenantMap(
            provider="github",
            provider_scope_id="personal:555:user-2",
            org_id=adp_default_id,
            install_metadata={
                "installation_id": 2002,
                "account_login": "user2-gh",
                "account_type": "User",
                "repository_selection": "selected",
                "repository_count": 2,
            },
        )
    )
    # Org connection in tenant-a
    db_session.add(_make_connection("tenant-a", "111", 1001, "alpha-gh"))
    await db_session.commit()

    with patch("src.admin.connections.adp_default.get_adp_default_org_id", return_value=adp_default_id):
        resp = await list_connections(
            caller_org_id="tenant-a",
            caller_user_id="user-1",
            db=db_session,
            member_tenant_ids=["tenant-a", adp_default_id],
        )

    # Should see: tenant-a org connection + only user-1's personal in adp-default
    assert len(resp.connections) == 2
    logins = {c.account_login for c in resp.connections}
    assert logins == {"alpha-gh", "user1-gh"}
    # user2-gh should be excluded
    assert "user2-gh" not in logins


@pytest.mark.asyncio
async def test_active_tenant_flagging(db_session: AsyncSession):
    """is_active_tenant correctly identifies the caller's current tenant."""
    from src.admin.connections.service import list_connections

    db_session.add_all(
        [
            _make_org("tenant-a", "Alpha Corp"),
            _make_org("tenant-b", "Beta Inc"),
        ]
    )
    db_session.add_all(
        [
            _make_connection("tenant-a", "111", 1001, "alpha-gh"),
            _make_connection("tenant-b", "222", 1002, "beta-gh"),
        ]
    )
    await db_session.commit()

    resp = await list_connections(
        caller_org_id="tenant-b",  # Active tenant is B
        caller_user_id="user-1",
        db=db_session,
        member_tenant_ids=["tenant-a", "tenant-b"],
    )

    by_login = {c.account_login: c for c in resp.connections}
    assert by_login["alpha-gh"].is_active_tenant is False
    assert by_login["beta-gh"].is_active_tenant is True


@pytest.mark.asyncio
async def test_route_level_cognito_sub_resolution(db_session: AsyncSession):
    """Route-level test: cognito_sub→users.id resolution produces multi-tenant results.

    Proves that the get_connections route correctly resolves the Postgres user_id
    from cognito_sub (not using cognito_sub directly against TenantMembership.user_id).
    """
    from src.admin.connections.routes import get_connections
    from src.shared.schemas.auth import TokenContext

    # Setup: User with cognito_sub different from users.id
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
    db_session.add_all(
        [
            _make_connection("tenant-a", "111", 1001, "alpha-gh"),
            _make_connection("tenant-b", "222", 1002, "beta-gh"),
        ]
    )
    await db_session.commit()

    # Mock the dependencies
    from datetime import UTC, datetime, timedelta

    token_context = TokenContext(
        user_id=cognito_sub,  # This is Cognito sub, NOT pg_user_id
        org_id="tenant-a",
        team_id="team-1",
        department_id="dept-1",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    # Call get_connections directly with the mocked dependencies
    with (
        patch("src.admin.connections.routes.get_current_user", return_value=token_context),
        patch("src.admin.connections.routes.get_db", return_value=db_session),
    ):
        resp = await get_connections(current_user=token_context, db=db_session)

    # Should see connections from both tenants (resolved via cognito_sub → users.id → memberships)
    assert len(resp.connections) == 2
    logins = {c.account_login for c in resp.connections}
    assert logins == {"alpha-gh", "beta-gh"}

    # Active tenant flagging should be correct
    by_login = {c.account_login: c for c in resp.connections}
    assert by_login["alpha-gh"].is_active_tenant is True
    assert by_login["beta-gh"].is_active_tenant is False
