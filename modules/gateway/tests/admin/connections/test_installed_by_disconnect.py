"""Unit tests for Issue #3073: installed_by_user_id gates disconnect.

Tests:
- Org install (nonce path) stores installed_by_user_id on the mapping row.
- No-nonce public install leaves installed_by_user_id NULL.
- Disconnect by installer WITHOUT admin role → allowed.
- Disconnect by non-installer member without admin role → 403.
- Disconnect by workspace admin who isn't installer → still allowed (regression).
- Cross-tenant disconnect attempt → still blocked (regression).
- can_manage computed correctly for installer / admin / plain member.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.connections.github_client import GitHubAppClient
from src.admin.connections.service import (
    _PROVIDER_GITHUB_INSTALL,
    delete_connection,
    install_callback,
    list_connections,
)
from src.shared.models.base import Base
from src.shared.models.organization import Organization, User
from src.shared.models.vault import ChannelTenantMap, MagicLinkNonce

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(autouse=True)
def _configure_github_app(monkeypatch):
    """Block Secrets Manager and DDB in unit tests."""
    from src.admin.connections.github_app_provider import _reset_provider_for_testing

    monkeypatch.setenv("BG_GITHUB_APP_SLUG", "test-adp-agent")
    _reset_provider_for_testing(None)
    with patch(
        "src.admin.connections.github_app_provider.boto3.client",
        side_effect=RuntimeError("Secrets Manager blocked in unit tests"),
    ):
        with patch(
            "src.admin.connections.service._write_installation_identity_index",
            new_callable=AsyncMock,
            return_value=None,
        ):
            yield
    _reset_provider_for_testing(None)


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


@pytest.fixture
async def second_org(db_session: AsyncSession) -> Organization:
    """Second org for cross-tenant tests."""
    org = Organization(
        id="org-test-002",
        name="Other Org",
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


async def _seed_user_and_nonce(
    db: AsyncSession,
    *,
    jti: str = "installed-by-jti",
    user_id: str = "user-installer-001",
    cognito_sub: str = "sub-installer",
    org_id: str = "org-test-001",
) -> tuple[User, MagicLinkNonce]:
    """Seed a User and a valid nonce pointing at them."""
    user = User(
        id=user_id,
        org_id=org_id,
        team_id="team-test-001",
        email=f"{user_id}@test.local",
        cognito_sub=cognito_sub,
    )
    db.add(user)
    await db.commit()

    nonce = MagicLinkNonce(
        jti=jti,
        provider=_PROVIDER_GITHUB_INSTALL,
        provider_user_id=cognito_sub,
        channel_context=None,
        target_user_id=user_id,
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
        consumed_at=None,
    )
    db.add(nonce)
    await db.commit()
    return user, nonce


async def _seed_mapping(
    db: AsyncSession,
    *,
    org_id: str = "org-test-001",
    installed_by_user_id: str | None = None,
    scope_id: str = "98765",
    installation_id: int = 124731131,
) -> ChannelTenantMap:
    """Seed a ChannelTenantMap row directly."""
    mapping = ChannelTenantMap(
        provider="github",
        provider_scope_id=scope_id,
        org_id=org_id,
        install_metadata={
            "installation_id": installation_id,
            "account_login": "sophos-test",
            "account_type": "Organization",
            "repository_selection": "selected",
            "repository_count": 2,
            "repositories": ["acme/repo-one", "acme/repo-two"],
        },
        installed_by_user_id=installed_by_user_id,
    )
    db.add(mapping)
    await db.commit()
    return mapping


# ---------------------------------------------------------------------------
# Tests: installed_by_user_id is stored correctly
# ---------------------------------------------------------------------------


class TestInstalledByStorage:
    """Issue #3073: install_callback stores installed_by_user_id on the mapping."""

    async def test_nonce_install_stores_installed_by(self, db_session: AsyncSession, org_in_db):
        """Org install via nonce path → mapping.installed_by_user_id = installer's PG id."""
        await _seed_user_and_nonce(db_session)
        gh = _mock_github_client()

        result = await install_callback(
            installation_id=124731131,
            setup_action="install",
            state="installed-by-jti",
            db=db_session,
            github_client=gh,
        )

        assert result["success"] is True

        # Verify installed_by_user_id was stored
        stmt = select(ChannelTenantMap).where(
            ChannelTenantMap.provider == "github",
            ChannelTenantMap.provider_scope_id == "98765",
        )
        mapping = (await db_session.execute(stmt)).scalar_one()
        assert mapping.installed_by_user_id == "user-installer-001"

    async def test_no_nonce_install_leaves_null(self, db_session: AsyncSession, org_in_db):
        """Public install (no nonce) → installed_by_user_id is NULL."""
        gh = _mock_github_client()

        # The no-nonce path fires when state is empty
        with patch(
            "src.admin.connections.service._upsert_org_tenant_shell",
            new_callable=AsyncMock,
            return_value="org-test-001",
        ):
            with patch.dict("os.environ", {"ORG_TENANT_AUTO_CREATE": "true"}):
                result = await install_callback(
                    installation_id=124731131,
                    setup_action="install",
                    state="",
                    db=db_session,
                    github_client=gh,
                )

        assert result.get("success") is True or result.get("no_nonce") is True

        # Verify installed_by_user_id is NULL
        stmt = select(ChannelTenantMap).where(
            ChannelTenantMap.provider == "github",
            ChannelTenantMap.provider_scope_id == "98765",
        )
        mapping = (await db_session.execute(stmt)).scalar_one_or_none()
        if mapping:
            assert mapping.installed_by_user_id is None

    async def test_reinstall_updates_installed_by(self, db_session: AsyncSession, org_in_db):
        """Re-install by a different verified user → installed_by is updated."""
        # First install by user A
        await _seed_user_and_nonce(db_session, jti="first-jti", user_id="user-a", cognito_sub="sub-a")
        gh = _mock_github_client()

        await install_callback(
            installation_id=124731131,
            setup_action="install",
            state="first-jti",
            db=db_session,
            github_client=gh,
        )

        # Seed user B with a new nonce
        user_b = User(
            id="user-b",
            org_id="org-test-001",
            team_id="team-test-001",
            email="user-b@test.local",
            cognito_sub="sub-b",
        )
        db_session.add(user_b)
        await db_session.commit()

        nonce_b = MagicLinkNonce(
            jti="second-jti",
            provider=_PROVIDER_GITHUB_INSTALL,
            provider_user_id="sub-b",
            channel_context=None,
            target_user_id="user-b",
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
            consumed_at=None,
        )
        db_session.add(nonce_b)
        await db_session.commit()

        # Re-install by user B
        await install_callback(
            installation_id=124731131,
            setup_action="install",
            state="second-jti",
            db=db_session,
            github_client=gh,
        )

        stmt = select(ChannelTenantMap).where(
            ChannelTenantMap.provider == "github",
            ChannelTenantMap.provider_scope_id == "98765",
        )
        mapping = (await db_session.execute(stmt)).scalar_one()
        assert mapping.installed_by_user_id == "user-b"


# ---------------------------------------------------------------------------
# Tests: delete_connection authorization
# ---------------------------------------------------------------------------


class TestDeleteConnectionAuthorization:
    """Issue #3073: disconnect allowed for installer or admin, denied otherwise."""

    async def test_installer_can_disconnect_without_admin(self, db_session: AsyncSession, org_in_db):
        """Installer (non-admin) can disconnect their own connection."""
        user = User(
            id="user-installer-001",
            org_id="org-test-001",
            team_id="team-test-001",
            email="installer@test.local",
            cognito_sub="sub-installer",
        )
        db_session.add(user)
        await db_session.commit()

        await _seed_mapping(db_session, installed_by_user_id="user-installer-001")
        gh = _mock_github_client()

        result = await delete_connection(
            installation_id=124731131,
            caller_org_id="org-test-001",
            db=db_session,
            github_client=gh,
            caller_user_id="user-installer-001",
            caller_is_admin=False,
        )

        assert result.deleted is True
        assert result.installation_id == 124731131

    async def test_non_installer_member_denied(self, db_session: AsyncSession, org_in_db):
        """Non-installer, non-admin member → PermissionError (403)."""
        await _seed_mapping(db_session, installed_by_user_id="user-installer-001")
        gh = _mock_github_client()

        with pytest.raises(PermissionError, match="do not have permission"):
            await delete_connection(
                installation_id=124731131,
                caller_org_id="org-test-001",
                db=db_session,
                github_client=gh,
                caller_user_id="user-other-member",
                caller_is_admin=False,
            )

    async def test_admin_who_is_not_installer_can_disconnect(self, db_session: AsyncSession, org_in_db):
        """Workspace admin who didn't install → still allowed (regression)."""
        await _seed_mapping(db_session, installed_by_user_id="user-installer-001")
        gh = _mock_github_client()

        result = await delete_connection(
            installation_id=124731131,
            caller_org_id="org-test-001",
            db=db_session,
            github_client=gh,
            caller_user_id="user-admin-999",
            caller_is_admin=True,
        )

        assert result.deleted is True

    async def test_cross_tenant_disconnect_blocked(self, db_session: AsyncSession, org_in_db, second_org):
        """Cross-tenant disconnect → still blocked even if user is installer."""
        await _seed_mapping(db_session, installed_by_user_id="user-installer-001")
        gh = _mock_github_client()

        with pytest.raises(PermissionError, match="different ADP tenant"):
            await delete_connection(
                installation_id=124731131,
                caller_org_id="org-test-002",
                db=db_session,
                github_client=gh,
                caller_user_id="user-installer-001",
                caller_is_admin=True,
            )

    async def test_null_installed_by_denies_non_admin(self, db_session: AsyncSession, org_in_db):
        """Connection with NULL installed_by → non-admin cannot disconnect."""
        await _seed_mapping(db_session, installed_by_user_id=None)
        gh = _mock_github_client()

        with pytest.raises(PermissionError, match="do not have permission"):
            await delete_connection(
                installation_id=124731131,
                caller_org_id="org-test-001",
                db=db_session,
                github_client=gh,
                caller_user_id="user-some-member",
                caller_is_admin=False,
            )

    async def test_null_installed_by_allows_admin(self, db_session: AsyncSession, org_in_db):
        """Connection with NULL installed_by → admin can still disconnect."""
        await _seed_mapping(db_session, installed_by_user_id=None)
        gh = _mock_github_client()

        result = await delete_connection(
            installation_id=124731131,
            caller_org_id="org-test-001",
            db=db_session,
            github_client=gh,
            caller_user_id="user-admin-999",
            caller_is_admin=True,
        )

        assert result.deleted is True


# ---------------------------------------------------------------------------
# Tests: can_manage computation in list_connections
# ---------------------------------------------------------------------------


class TestCanManageComputation:
    """Issue #3073: can_manage is computed correctly for each connection."""

    async def test_admin_always_can_manage(self, db_session: AsyncSession, org_in_db):
        """Admin user → can_manage=True regardless of installed_by."""
        await _seed_mapping(db_session, installed_by_user_id="user-other")
        gh = _mock_github_client()

        result = await list_connections(
            caller_org_id="org-test-001",
            caller_user_id="sub-admin",
            db=db_session,
            github_client=gh,
            caller_is_admin=True,
            caller_pg_user_id="user-admin-999",
        )

        assert len(result.connections) == 1
        assert result.connections[0].can_manage is True

    async def test_installer_can_manage(self, db_session: AsyncSession, org_in_db):
        """Non-admin installer → can_manage=True for their connection."""
        await _seed_mapping(db_session, installed_by_user_id="user-installer-001")
        gh = _mock_github_client()

        result = await list_connections(
            caller_org_id="org-test-001",
            caller_user_id="sub-installer",
            db=db_session,
            github_client=gh,
            caller_is_admin=False,
            caller_pg_user_id="user-installer-001",
        )

        assert len(result.connections) == 1
        assert result.connections[0].can_manage is True

    async def test_plain_member_cannot_manage(self, db_session: AsyncSession, org_in_db):
        """Non-admin, non-installer member → can_manage=False."""
        await _seed_mapping(db_session, installed_by_user_id="user-installer-001")
        gh = _mock_github_client()

        result = await list_connections(
            caller_org_id="org-test-001",
            caller_user_id="sub-plain",
            db=db_session,
            github_client=gh,
            caller_is_admin=False,
            caller_pg_user_id="user-plain-member",
        )

        assert len(result.connections) == 1
        assert result.connections[0].can_manage is False

    async def test_null_installed_by_non_admin_cannot_manage(self, db_session: AsyncSession, org_in_db):
        """Connection with NULL installed_by + non-admin → can_manage=False."""
        await _seed_mapping(db_session, installed_by_user_id=None)
        gh = _mock_github_client()

        result = await list_connections(
            caller_org_id="org-test-001",
            caller_user_id="sub-member",
            db=db_session,
            github_client=gh,
            caller_is_admin=False,
            caller_pg_user_id="user-member",
        )

        assert len(result.connections) == 1
        assert result.connections[0].can_manage is False
