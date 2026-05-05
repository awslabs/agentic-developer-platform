"""Unit tests for the adp-default fallback tenant helpers.

Issue #466: Personal GitHub accounts land in adp-default with per-user scoping.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.connections.adp_default import (
    attach_to_adp_default,
    get_adp_default_org_id,
    is_adp_default,
)
from src.shared.models.base import Base
from src.shared.models.organization import Organization
from src.shared.models.vault import ChannelTenantMap

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def db_engine():
    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        import src.admin.models  # noqa: F401
        import src.shared.models.organization  # noqa: F401
        import src.shared.models.vault  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine) -> AsyncSession:
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def adp_default_org(db_session: AsyncSession) -> Organization:
    """Seed the adp-default org row (same as the seed script would do)."""
    org = Organization(
        id=get_adp_default_org_id(),
        name="Free tier",
        aws_accounts=[],
        role_mappings={},
        settings={"tier": "free"},
    )
    db_session.add(org)
    await db_session.commit()
    return org


# ---------------------------------------------------------------------------
# is_adp_default / get_adp_default_org_id
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_get_adp_default_org_id_returns_well_known_uuid(self):
        result = get_adp_default_org_id()
        assert result == "00000000-0000-4000-a000-000000000001"

    def test_is_adp_default_true(self):
        assert is_adp_default("00000000-0000-4000-a000-000000000001") is True

    def test_is_adp_default_false(self):
        assert is_adp_default("org-some-paid-tenant") is False


# ---------------------------------------------------------------------------
# attach_to_adp_default
# ---------------------------------------------------------------------------


class TestAttachToAdpDefault:
    async def test_new_installation_creates_mapping(self, db_session: AsyncSession, adp_default_org):
        """A new personal install creates a ChannelTenantMap row bound to adp-default."""
        result = await attach_to_adp_default(
            installation_id=42,
            account_login="alice",
            github_account_id=12345,
            caller_user_id="user-001",
            db=db_session,
        )

        assert result["success"] is True
        assert result["tenant"] == "adp-default"
        assert result["user_id"] == "user-001"
        assert result["installation_id"] == 42

        # Verify the DB row
        from sqlalchemy import select

        stmt = select(ChannelTenantMap).where(
            ChannelTenantMap.provider == "github",
            ChannelTenantMap.provider_scope_id == "personal:12345:user-001",
        )
        row = (await db_session.execute(stmt)).scalar_one_or_none()
        assert row is not None
        assert row.org_id == get_adp_default_org_id()

    async def test_idempotent_reinstall_by_same_user(self, db_session: AsyncSession, adp_default_org):
        """Re-installing the same GitHub account by the same user succeeds without duplicates."""
        await attach_to_adp_default(
            installation_id=42,
            account_login="alice",
            github_account_id=12345,
            caller_user_id="user-001",
            db=db_session,
        )
        # Second call — idempotent
        result = await attach_to_adp_default(
            installation_id=43,  # different installation_id (reinstall)
            account_login="alice",
            github_account_id=12345,
            caller_user_id="user-001",
            db=db_session,
        )

        assert result["success"] is True

        # Only one mapping row
        from sqlalchemy import func, select

        stmt = (
            select(func.count())
            .select_from(ChannelTenantMap)
            .where(
                ChannelTenantMap.provider == "github",
                ChannelTenantMap.provider_scope_id.like("personal:12345:%"),
            )
        )
        count = (await db_session.execute(stmt)).scalar()
        assert count == 1

    async def test_different_user_same_github_account_raises_409(self, db_session: AsyncSession, adp_default_org):
        """User B cannot claim a GitHub account already connected by User A in adp-default."""
        # User A installs
        await attach_to_adp_default(
            installation_id=42,
            account_login="alice",
            github_account_id=12345,
            caller_user_id="user-001",
            db=db_session,
        )

        # User B tries the same GitHub account
        with pytest.raises(PermissionError, match="already connected under a different ADP user"):
            await attach_to_adp_default(
                installation_id=42,
                account_login="alice",
                github_account_id=12345,
                caller_user_id="user-002",
                db=db_session,
            )

    async def test_same_user_different_github_accounts_both_succeed(self, db_session: AsyncSession, adp_default_org):
        """A user can install the app on multiple personal repos."""
        r1 = await attach_to_adp_default(
            installation_id=42,
            account_login="alice",
            github_account_id=12345,
            caller_user_id="user-001",
            db=db_session,
        )
        r2 = await attach_to_adp_default(
            installation_id=43,
            account_login="alice-alt",
            github_account_id=67890,
            caller_user_id="user-001",
            db=db_session,
        )

        assert r1["success"] is True
        assert r2["success"] is True

    async def test_github_account_claimed_by_paid_tenant_raises(self, db_session: AsyncSession, adp_default_org):
        """If a GitHub account is already mapped to a paid tenant, reject."""
        # Create a paid org
        paid_org = Organization(
            id="org-paid-001",
            name="Paid Org",
            aws_accounts=[],
            role_mappings={},
            settings={},
        )
        db_session.add(paid_org)
        await db_session.commit()

        # Pre-existing mapping owned by paid org (using numeric scope_id like the org flow does)
        existing = ChannelTenantMap(
            provider="github",
            provider_scope_id="12345",
            org_id="org-paid-001",
        )
        db_session.add(existing)
        await db_session.commit()

        with pytest.raises(PermissionError, match="already connected to another ADP tenant"):
            await attach_to_adp_default(
                installation_id=42,
                account_login="alice",
                github_account_id=12345,
                caller_user_id="user-001",
                db=db_session,
            )

    async def test_fallback_to_login_when_github_id_none(self, db_session: AsyncSession, adp_default_org):
        """When GitHub doesn't provide a numeric ID, uses login as scope key."""
        result = await attach_to_adp_default(
            installation_id=42,
            account_login="bob",
            github_account_id=None,
            caller_user_id="user-001",
            db=db_session,
        )

        assert result["success"] is True

        from sqlalchemy import select

        stmt = select(ChannelTenantMap).where(
            ChannelTenantMap.provider == "github",
            ChannelTenantMap.provider_scope_id == "personal:bob:user-001",
        )
        row = (await db_session.execute(stmt)).scalar_one_or_none()
        assert row is not None


# ---------------------------------------------------------------------------
# Integration: install_callback dispatches to adp-default for User type
# ---------------------------------------------------------------------------


class TestInstallCallbackDispatch:
    """Verify install_callback correctly routes personal accounts to adp-default."""

    async def _write_nonce(self, db: AsyncSession, jti: str = "test-jti") -> None:
        from datetime import UTC, datetime, timedelta

        from src.shared.models.vault import MagicLinkNonce

        nonce = MagicLinkNonce(
            jti=jti,
            provider="github_install",
            provider_user_id="sub-abc",
            channel_context=None,
            target_user_id="user-001",
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
            consumed_at=None,
        )
        db.add(nonce)
        await db.commit()

    async def test_personal_account_calls_attach_to_adp_default(self, db_session: AsyncSession, adp_default_org):
        """install_callback with account_type=User dispatches to adp-default flow."""
        from unittest.mock import AsyncMock, MagicMock

        from src.admin.connections.github_client import GitHubAppClient
        from src.admin.connections.service import install_callback

        await self._write_nonce(db_session, jti="personal-jti")

        gh = MagicMock(spec=GitHubAppClient)
        gh.get_installation = AsyncMock(
            return_value={
                "id": 999,
                "account": {
                    "type": "User",
                    "login": "alice",
                    "id": 12345,
                },
                "repository_selection": "all",
                "created_at": "2026-05-01T10:00:00Z",
            }
        )

        result = await install_callback(
            installation_id=999,
            setup_action="install",
            state="personal-jti",
            caller_user_id="user-001",
            caller_org_id=get_adp_default_org_id(),
            db=db_session,
            github_client=gh,
        )

        assert result["success"] is True
        assert result["account_type"] == "User"

        # Verify mapping was created
        from sqlalchemy import select

        stmt = select(ChannelTenantMap).where(
            ChannelTenantMap.provider == "github",
            ChannelTenantMap.provider_scope_id == "personal:12345:user-001",
        )
        row = (await db_session.execute(stmt)).scalar_one_or_none()
        assert row is not None

    async def test_org_account_does_not_call_adp_default(self, db_session: AsyncSession, adp_default_org):
        """install_callback with account_type=Organization uses normal org flow."""
        from unittest.mock import AsyncMock, MagicMock

        from src.admin.connections.github_client import GitHubAppClient
        from src.admin.connections.service import install_callback

        # Create the caller's paid org
        paid_org = Organization(
            id="org-paid-002",
            name="My Corp",
            aws_accounts=[],
            role_mappings={},
            settings={},
        )
        db_session.add(paid_org)
        await db_session.commit()

        await self._write_nonce(db_session, jti="org-jti")

        gh = MagicMock(spec=GitHubAppClient)
        gh.get_installation = AsyncMock(
            return_value={
                "id": 888,
                "account": {
                    "type": "Organization",
                    "login": "my-corp",
                    "id": 77777,
                },
                "repository_selection": "selected",
                "created_at": "2026-05-01T10:00:00Z",
            }
        )

        result = await install_callback(
            installation_id=888,
            setup_action="install",
            state="org-jti",
            caller_user_id="user-001",
            caller_org_id="org-paid-002",
            db=db_session,
            github_client=gh,
        )

        assert result["success"] is True
        assert result["account_type"] == "Organization"

        # Verify org mapping was created (not personal mapping)
        from sqlalchemy import select

        stmt = select(ChannelTenantMap).where(
            ChannelTenantMap.provider == "github",
            ChannelTenantMap.provider_scope_id == "77777",
        )
        row = (await db_session.execute(stmt)).scalar_one_or_none()
        assert row is not None
        assert row.org_id == "org-paid-002"
