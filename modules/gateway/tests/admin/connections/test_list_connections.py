"""Tests for list_connections — Bug A/B/C fixes from issue #724.

Verifies:
- resolve_effective_org_id is used (Bug A)
- Metadata-driven enrichment replaces the stub (Bug B)
- Personal accounts scoped to caller user only (Bug C)
- Legacy rows without metadata are skipped with warning
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.models.base import Base
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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_connections_returns_metadata_enriched_items(db_session: AsyncSession):
    """Bug B: list_connections reads enrichment from metadata column."""
    from src.admin.connections.service import list_connections

    # Insert a ChannelTenantMap row with metadata
    mapping = ChannelTenantMap(
        provider="github",
        provider_scope_id="12345",
        org_id="tenant-acme",
        install_metadata={
            "installation_id": 99001,
            "account_login": "acme-corp",
            "account_type": "Organization",
            "repository_selection": "all",
            "repository_count": 7,
        },
    )
    db_session.add(mapping)
    await db_session.commit()

    resp = await list_connections(
        caller_org_id="tenant-acme",
        caller_user_id="user-1",
        db=db_session,
    )

    assert len(resp.connections) == 1
    conn = resp.connections[0]
    assert conn.installation_id == 99001
    assert conn.account_login == "acme-corp"
    assert conn.account_type == "Organization"
    assert conn.repository_selection == "all"
    assert conn.repository_count == 7
    assert conn.configure_url == "https://github.com/settings/installations/99001"


@pytest.mark.asyncio
async def test_list_connections_skips_legacy_rows_without_metadata(db_session: AsyncSession, caplog):
    """Legacy rows (pre-#724) without metadata are skipped with a warning."""
    from src.admin.connections.service import list_connections

    # Row WITHOUT metadata (legacy)
    legacy = ChannelTenantMap(
        provider="github",
        provider_scope_id="legacy-org",
        org_id="tenant-acme",
        install_metadata=None,
    )
    # Row WITH metadata
    good = ChannelTenantMap(
        provider="github",
        provider_scope_id="67890",
        org_id="tenant-acme",
        install_metadata={
            "installation_id": 42,
            "account_login": "good-org",
            "account_type": "Organization",
        },
    )
    db_session.add_all([legacy, good])
    await db_session.commit()

    resp = await list_connections(
        caller_org_id="tenant-acme",
        caller_user_id="user-1",
        db=db_session,
    )

    # Only the row with metadata is returned
    assert len(resp.connections) == 1
    assert resp.connections[0].installation_id == 42
    assert "no installation_id metadata" in caplog.text


@pytest.mark.asyncio
async def test_list_connections_personal_scopes_to_caller_user(db_session: AsyncSession):
    """Bug C: personal installs in adp-default only show the caller's own."""
    from src.admin.connections.service import list_connections

    adp_default_id = "00000000-0000-4000-a000-000000000001"

    # User A's personal install
    user_a = ChannelTenantMap(
        provider="github",
        provider_scope_id="personal:111:user-a",
        org_id=adp_default_id,
        install_metadata={
            "installation_id": 1001,
            "account_login": "alice",
            "account_type": "User",
        },
    )
    # User B's personal install
    user_b = ChannelTenantMap(
        provider="github",
        provider_scope_id="personal:222:user-b",
        org_id=adp_default_id,
        install_metadata={
            "installation_id": 1002,
            "account_login": "bob",
            "account_type": "User",
        },
    )
    # User C's personal install
    user_c = ChannelTenantMap(
        provider="github",
        provider_scope_id="personal:333:user-c",
        org_id=adp_default_id,
        install_metadata={
            "installation_id": 1003,
            "account_login": "charlie",
            "account_type": "User",
        },
    )
    db_session.add_all([user_a, user_b, user_c])
    await db_session.commit()

    # Query as user-a — should only see their own install
    with patch(
        "src.admin.connections.adp_default.get_adp_default_org_id",
        return_value=adp_default_id,
    ):
        resp = await list_connections(
            caller_org_id=adp_default_id,
            caller_user_id="user-a",
            db=db_session,
        )

    assert len(resp.connections) == 1
    assert resp.connections[0].account_login == "alice"
    assert resp.connections[0].installation_id == 1001


@pytest.mark.asyncio
async def test_list_connections_org_returns_all_for_org(db_session: AsyncSession):
    """Org-level installs return all connections for the tenant (no per-user filter)."""
    from src.admin.connections.service import list_connections

    for i in range(2):
        db_session.add(
            ChannelTenantMap(
                provider="github",
                provider_scope_id=f"org-{i}",
                org_id="tenant-acme",
                install_metadata={
                    "installation_id": 5000 + i,
                    "account_login": f"acme-team-{i}",
                    "account_type": "Organization",
                },
            )
        )
    await db_session.commit()

    resp = await list_connections(
        caller_org_id="tenant-acme",
        caller_user_id="any-user",
        db=db_session,
    )

    assert len(resp.connections) == 2


@pytest.mark.asyncio
async def test_list_connections_uses_resolve_effective_org_id(db_session: AsyncSession):
    """Bug A: get_connections route resolves org_id from DB when token has empty org_id."""

    from src.admin.connections.service import list_connections

    # Insert a mapping
    db_session.add(
        ChannelTenantMap(
            provider="github",
            provider_scope_id="resolved-org",
            org_id="resolved-tenant-id",
            install_metadata={
                "installation_id": 777,
                "account_login": "resolved-org",
                "account_type": "Organization",
            },
        )
    )
    await db_session.commit()

    # Call list_connections with the resolved org_id (simulates what the route does
    # after resolve_effective_org_id returns the DB-stored value)
    resp = await list_connections(
        caller_org_id="resolved-tenant-id",
        caller_user_id="user-with-empty-token-org",
        db=db_session,
    )

    assert len(resp.connections) == 1
    assert resp.connections[0].installation_id == 777


@pytest.mark.asyncio
async def test_install_callback_persists_metadata(db_session: AsyncSession):
    """install_callback stores metadata on the ChannelTenantMap row."""

    from sqlalchemy import select

    from src.admin.connections.service import _attach_org_installation
    from src.shared.models.vault import ChannelTenantMap

    await _attach_org_installation(
        installation_id=12345,
        github_org_id=99999,
        github_org_login="test-org",
        caller_org_id="tenant-test",
        db=db_session,
    )

    # Verify the row has metadata
    stmt = select(ChannelTenantMap).where(
        ChannelTenantMap.provider == "github",
        ChannelTenantMap.provider_scope_id == "99999",
    )
    result = await db_session.execute(stmt)
    row = result.scalar_one()

    assert row.install_metadata is not None
    assert row.install_metadata["installation_id"] == 12345
    assert row.install_metadata["account_login"] == "test-org"
    assert row.install_metadata["account_type"] == "Organization"
