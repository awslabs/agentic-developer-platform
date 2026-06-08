"""Unit tests for the connections service layer.

Issue #465: GitHub App install + connection management.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.connections.github_client import GitHubAppClient
from src.admin.connections.service import (
    _PROVIDER_GITHUB_INSTALL,
    delete_connection,
    install_callback,
    install_start,
    list_connections,
)
from src.shared.models.base import Base
from src.shared.models.organization import Organization
from src.shared.models.vault import ChannelTenantMap, MagicLinkNonce

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(autouse=True)
def _configure_github_app(monkeypatch):
    """The App slug is now required deployment config (no hardcoded fallback).

    get_settings() reads BG_GITHUB_APP_SLUG from the environment fresh on each
    call, so setting the env var here makes install_start() resolve a real slug.
    Tests that assert the unconfigured 503 path override this explicitly.
    """
    monkeypatch.setenv("BG_GITHUB_APP_SLUG", "test-adp-agent")


@pytest.fixture
async def db_engine():
    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        # Import all models so metadata is complete
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
async def org_in_db(db_session: AsyncSession) -> Organization:
    """Create a minimal org row required by ChannelTenantMap FK."""
    org = Organization(
        id="org-test-001",
        name="Test Org",
        aws_accounts=[],
        role_mappings={},
        settings={},
    )
    db_session.add(org)
    await db_session.commit()
    return org


def _mock_github_client(
    *,
    installation_id: int = 124731131,
    account_login: str = "sophos-test",
    account_type: str = "Organization",
    account_github_id: int = 98765,
) -> MagicMock:
    client = MagicMock(spec=GitHubAppClient)
    client.get_installation = AsyncMock(
        return_value={
            "id": installation_id,
            "account": {
                "type": account_type,
                "login": account_login,
                "id": account_github_id,
            },
            "repository_selection": "selected",
            "created_at": "2026-05-01T10:00:00Z",
        }
    )
    client.delete_installation = AsyncMock(return_value=None)
    client.list_installation_repositories = AsyncMock(return_value=2)
    client.list_installation_repository_names = AsyncMock(return_value=["acme/repo-one", "acme/repo-two"])
    return client


# ---------------------------------------------------------------------------
# install_start
# ---------------------------------------------------------------------------


class TestInstallStart:
    async def test_writes_nonce_with_correct_provider(self, db_session: AsyncSession):
        result = await install_start(
            cognito_sub="sub-abc",
            user_id="user-001",
            db=db_session,
        )

        assert result.state_token
        assert "github_install" in result.install_url or result.state_token in result.install_url
        assert result.expires_at > datetime.now(UTC)

        # Verify nonce row written to DB
        from sqlalchemy import select

        stmt = select(MagicLinkNonce).where(MagicLinkNonce.jti == result.state_token)
        row = (await db_session.execute(stmt)).scalar_one_or_none()
        assert row is not None
        assert row.provider == _PROVIDER_GITHUB_INSTALL
        assert row.provider_user_id == "sub-abc"
        assert row.target_user_id == "user-001"
        assert row.consumed_at is None

    async def test_nonce_ttl_is_15_minutes(self, db_session: AsyncSession):
        before = datetime.now(UTC)
        result = await install_start(
            cognito_sub="sub-abc",
            user_id="user-001",
            db=db_session,
        )
        after = datetime.now(UTC)

        expected_low = before + timedelta(seconds=890)
        expected_high = after + timedelta(seconds=910)
        assert expected_low <= result.expires_at <= expected_high

    async def test_install_url_contains_state(self, db_session: AsyncSession):
        result = await install_start(
            cognito_sub="sub-abc",
            user_id="user-001",
            db=db_session,
        )
        assert f"state={result.state_token}" in result.install_url

    async def test_install_url_uses_configured_slug(self, db_session: AsyncSession, monkeypatch):
        """The install URL is built from BG_GITHUB_APP_SLUG (deployment config),
        not a hardcoded app name — so each deployment points at its own App."""
        monkeypatch.setenv("BG_GITHUB_APP_SLUG", "my-org-adp-agent")
        result = await install_start(cognito_sub="sub-abc", user_id="user-001", db=db_session)
        assert "github.com/apps/my-org-adp-agent/installations/new" in result.install_url

    async def test_raises_503_when_app_slug_unconfigured(self, db_session: AsyncSession, monkeypatch):
        """No hardcoded fallback: a blank slug must fail loudly rather than point
        the install button at the wrong App."""
        from fastapi import HTTPException

        monkeypatch.delenv("BG_GITHUB_APP_SLUG", raising=False)
        monkeypatch.setenv("BG_GITHUB_APP_SLUG", "")
        with pytest.raises(HTTPException) as exc_info:
            await install_start(cognito_sub="sub-abc", user_id="user-001", db=db_session)
        assert exc_info.value.status_code == 503
        assert "BG_GITHUB_APP_SLUG" in str(exc_info.value.detail)

    async def test_each_call_generates_unique_nonce(self, db_session: AsyncSession):
        r1 = await install_start(cognito_sub="sub-abc", user_id="user-001", db=db_session)
        r2 = await install_start(cognito_sub="sub-abc", user_id="user-001", db=db_session)
        assert r1.state_token != r2.state_token


# ---------------------------------------------------------------------------
# install_callback
# ---------------------------------------------------------------------------


class TestInstallCallback:
    async def _write_nonce(
        self,
        db: AsyncSession,
        *,
        jti: str = "test-jti-001",
        target_user_id: str = "user-001",
        expired: bool = False,
        consumed: bool = False,
        seed_user: bool = True,
        user_org_id: str = "org-test-001",
    ) -> MagicLinkNonce:
        now = datetime.now(UTC)
        # The callback resolves the caller's org from the users table via the
        # nonce's target_user_id, so seed a matching User (unless the test wants
        # to exercise the no-user path).
        if seed_user:
            from src.shared.models.organization import User

            existing = await db.get(User, target_user_id)
            if existing is None:
                db.add(
                    User(
                        id=target_user_id,
                        org_id=user_org_id,
                        team_id="team-test-001",
                        email=f"{target_user_id}@test.local",
                        cognito_sub="sub-abc",
                    )
                )
                await db.commit()
        nonce = MagicLinkNonce(
            jti=jti,
            provider=_PROVIDER_GITHUB_INSTALL,
            provider_user_id="sub-abc",
            channel_context=None,
            target_user_id=target_user_id,
            expires_at=now - timedelta(minutes=1) if expired else now + timedelta(minutes=15),
            consumed_at=now if consumed else None,
        )
        db.add(nonce)
        await db.commit()
        return nonce

    async def test_rejects_expired_nonce(self, db_session: AsyncSession, org_in_db):
        await self._write_nonce(db_session, jti="exp-jti", expired=True)

        from src.auth.magic_link import TokenExpiredError

        with pytest.raises(TokenExpiredError):
            await install_callback(
                installation_id=100,
                setup_action="install",
                state="exp-jti",
                db=db_session,
            )

    async def test_rejects_consumed_nonce(self, db_session: AsyncSession, org_in_db):
        await self._write_nonce(db_session, jti="con-jti", consumed=True)

        from src.auth.magic_link import NonceAlreadyConsumedError

        with pytest.raises(NonceAlreadyConsumedError):
            await install_callback(
                installation_id=100,
                setup_action="install",
                state="con-jti",
                db=db_session,
            )

    async def test_rejects_missing_nonce(self, db_session: AsyncSession, org_in_db):
        from src.auth.magic_link import NonceNotFoundError

        with pytest.raises(NonceNotFoundError):
            await install_callback(
                installation_id=100,
                setup_action="install",
                state="nonexistent-jti",
                db=db_session,
            )

    async def test_rejects_nonce_with_no_matching_user(self, db_session: AsyncSession, org_in_db):
        """The nonce is the authenticator; if it points at a user that no longer
        exists (and no cognito_sub match), the caller can't be resolved → reject."""
        await self._write_nonce(db_session, jti="orphan-jti", target_user_id="ghost-user", seed_user=False)

        from src.auth.magic_link import TargetUserMismatchError

        with pytest.raises(TargetUserMismatchError):
            await install_callback(
                installation_id=100,
                setup_action="install",
                state="orphan-jti",
                db=db_session,
            )

    async def test_successful_org_installation(self, db_session: AsyncSession, org_in_db):
        await self._write_nonce(db_session, jti="ok-jti")
        gh = _mock_github_client()

        result = await install_callback(
            installation_id=124731131,
            setup_action="install",
            state="ok-jti",
            db=db_session,
            github_client=gh,
        )

        assert result["success"] is True
        assert result["account_login"] == "sophos-test"
        assert result["account_type"] == "Organization"

    async def test_nonce_consumed_after_success(self, db_session: AsyncSession, org_in_db):
        from sqlalchemy import select

        await self._write_nonce(db_session, jti="consume-jti")
        gh = _mock_github_client()

        await install_callback(
            installation_id=124731131,
            setup_action="install",
            state="consume-jti",
            db=db_session,
            github_client=gh,
        )

        stmt = select(MagicLinkNonce).where(MagicLinkNonce.jti == "consume-jti")
        nonce = (await db_session.execute(stmt)).scalar_one()
        assert nonce.consumed_at is not None

    async def test_cross_tenant_conflict_raises_permission_error(self, db_session: AsyncSession, org_in_db):
        # Add a second org
        other_org = Organization(
            id="org-other-001",
            name="Other Org",
            aws_accounts=[],
            role_mappings={},
            settings={},
        )
        db_session.add(other_org)
        await db_session.commit()

        # Pre-existing mapping owned by the other org
        existing = ChannelTenantMap(
            provider="github",
            provider_scope_id="98765",  # same github_org_id as the mock client
            org_id="org-other-001",
        )
        db_session.add(existing)
        await db_session.commit()

        await self._write_nonce(db_session, jti="conflict-jti")
        gh = _mock_github_client(account_github_id=98765)

        with pytest.raises(PermissionError, match="already connected to another ADP tenant"):
            await install_callback(
                installation_id=124731131,
                setup_action="install",
                state="conflict-jti",
                db=db_session,
                github_client=gh,
            )

    async def test_personal_account_does_not_raise(self, db_session: AsyncSession, org_in_db):
        """Personal account installs are handed off to #466; must not raise."""
        await self._write_nonce(db_session, jti="personal-jti")
        gh = _mock_github_client(account_type="User", account_login="alice")

        result = await install_callback(
            installation_id=999,
            setup_action="install",
            state="personal-jti",
            db=db_session,
            github_client=gh,
        )
        assert result["success"] is True
        assert result["account_type"] == "User"


# ---------------------------------------------------------------------------
# list_connections
# ---------------------------------------------------------------------------


class TestListConnections:
    async def test_returns_empty_list_when_no_mappings(self, db_session: AsyncSession, org_in_db):
        result = await list_connections(caller_org_id="org-test-001", caller_user_id="user-1", db=db_session)
        assert result.connections == []

    async def test_returns_connections_for_tenant(self, db_session: AsyncSession, org_in_db):
        mapping = ChannelTenantMap(
            provider="github",
            provider_scope_id="98765",
            org_id="org-test-001",
            install_metadata={
                "installation_id": 98765,
                "account_login": "test-org",
                "account_type": "Organization",
            },
        )
        db_session.add(mapping)
        await db_session.commit()

        result = await list_connections(caller_org_id="org-test-001", caller_user_id="user-1", db=db_session)
        assert len(result.connections) == 1
        assert result.connections[0].provider == "github"

    async def test_does_not_return_other_tenants_connections(self, db_session: AsyncSession, org_in_db):
        other_org = Organization(
            id="org-other-002",
            name="Other Org",
            aws_accounts=[],
            role_mappings={},
            settings={},
        )
        db_session.add(other_org)
        mapping = ChannelTenantMap(
            provider="github",
            provider_scope_id="11111",
            org_id="org-other-002",
            install_metadata={
                "installation_id": 11111,
                "account_login": "other-org",
                "account_type": "Organization",
            },
        )
        db_session.add(mapping)
        await db_session.commit()

        result = await list_connections(caller_org_id="org-test-001", caller_user_id="user-1", db=db_session)
        assert result.connections == []


# ---------------------------------------------------------------------------
# delete_connection
# ---------------------------------------------------------------------------


class TestDeleteConnection:
    async def test_delete_requires_admin_ownership(self, db_session: AsyncSession, org_in_db):
        other_org = Organization(
            id="org-other-003",
            name="Other Org",
            aws_accounts=[],
            role_mappings={},
            settings={},
        )
        db_session.add(other_org)
        mapping = ChannelTenantMap(
            provider="github",
            provider_scope_id="98765",
            org_id="org-other-003",
        )
        db_session.add(mapping)
        await db_session.commit()

        gh = _mock_github_client(account_github_id=98765)
        with pytest.raises(PermissionError):
            await delete_connection(
                installation_id=124731131,  # different tenant
                caller_org_id="org-test-001",
                db=db_session,
                github_client=gh,
            )

    async def test_delete_not_found_raises_value_error(self, db_session: AsyncSession, org_in_db):
        gh = _mock_github_client(account_github_id=99999)
        with pytest.raises(ValueError):
            await delete_connection(
                installation_id=9999,
                caller_org_id="org-test-001",
                db=db_session,
                github_client=gh,
            )

    async def test_successful_delete_removes_mapping(self, db_session: AsyncSession, org_in_db):
        from sqlalchemy import select

        mapping = ChannelTenantMap(
            provider="github",
            provider_scope_id="98765",
            org_id="org-test-001",
        )
        db_session.add(mapping)
        await db_session.commit()

        gh = _mock_github_client(account_github_id=98765)
        result = await delete_connection(
            installation_id=124731131,
            caller_org_id="org-test-001",
            db=db_session,
            github_client=gh,
        )

        assert result.deleted is True
        assert result.installation_id == 124731131

        # Verify row removed
        stmt = select(ChannelTenantMap).where(
            ChannelTenantMap.provider == "github",
            ChannelTenantMap.provider_scope_id == "98765",
        )
        row = (await db_session.execute(stmt)).scalar_one_or_none()
        assert row is None

    async def test_successful_delete_calls_github_api(self, db_session: AsyncSession, org_in_db):
        mapping = ChannelTenantMap(
            provider="github",
            provider_scope_id="98765",
            org_id="org-test-001",
        )
        db_session.add(mapping)
        await db_session.commit()

        gh = _mock_github_client(account_github_id=98765)
        await delete_connection(
            installation_id=124731131,
            caller_org_id="org-test-001",
            db=db_session,
            github_client=gh,
        )

        gh.delete_installation.assert_called_once_with(124731131)
