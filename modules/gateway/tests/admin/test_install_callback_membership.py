"""GitHub connection setup must not create or promote human ADP memberships.

Provider control and canonical identity checks remain required."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.connections.github_client import GitHubAppClient
from src.admin.connections.service import (
    install_callback,
)
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import MagicLinkNonce
from tests.admin import install_setup_fixtures as setup_fixtures
from tests.admin.install_setup_fixtures import (
    bind_real_org_control,
    issue_install_nonce,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

offline_setup_boundaries = setup_fixtures.offline_setup_boundaries

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(autouse=True)
def _configure_github_app(monkeypatch, offline_setup_boundaries):
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


def _mock_github_client(
    *,
    installation_id: int = 124731131,
    account_login: str = "acme-test",
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
    # These human-routing cases model an unavailable optional bot lookup.
    client.get_bot_user = AsyncMock(return_value={})
    return bind_real_org_control(client)


async def _seed_user_and_nonce(
    db: AsyncSession,
    *,
    jti: str = "membership-jti",
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

    nonce = await issue_install_nonce(db, user, jti=jti)
    return user, nonce


# ---------------------------------------------------------------------------
# Tests: install callback creates membership for org installs
# ---------------------------------------------------------------------------


class TestInstallCallbackMembership:
    """Connecting a provider never grants an ADP role or changes membership."""

    @pytest.mark.parametrize("role", [None, "member", "org_admin"])
    async def test_org_install_preserves_existing_membership(self, db_session, org_in_db, role):
        user, _ = await _seed_user_and_nonce(db_session)
        if role:
            db_session.add(TenantMembership(user_id=user.id, tenant_id=user.org_id, role=role, is_active=True, joined_via="admin"))
            await db_session.commit()
        for jti in ["membership-jti", "reinstall-jti"]:
            if jti == "reinstall-jti":
                await issue_install_nonce(db_session, user, jti=jti)
            result = await install_callback(
                installation_id=124731131, setup_action="install", state=jti, db=db_session, github_client=_mock_github_client()
            )
            assert result["success"] is True
            assert result["switched_from"] is None
        rows = (await db_session.scalars(select(TenantMembership).where(TenantMembership.user_id == user.id))).all()
        assert [(m.tenant_id, m.role, m.is_active, m.joined_via) for m in rows] == ([(user.org_id, role, True, "admin")] if role else [])

    async def test_canonical_user_is_recorded_without_membership_grant(self, db_session, org_in_db):
        from src.shared.models.vault import ChannelTenantMap

        user, _ = await _seed_user_and_nonce(db_session, user_id="pg-uuid-001", cognito_sub="different-cognito-sub")
        result = await install_callback(
            installation_id=124731131, setup_action="install", state="membership-jti", db=db_session, github_client=_mock_github_client()
        )
        assert result["success"] is True
        row = (await db_session.scalars(select(ChannelTenantMap))).one()
        assert row.org_id == user.org_id
        assert row.installed_by_user_id == user.id
        assert (await db_session.scalars(select(TenantMembership))).all() == []


# ---------------------------------------------------------------------------
# Tests: check_org_membership diagnostic logging
# ---------------------------------------------------------------------------


class TestCheckOrgMembershipDiagnostic:
    """Issue #3035: check_org_membership logs WARNING on 302/403."""

    async def test_302_logs_warning(self, caplog):
        """302 redirect (members endpoint without permission) logs a warning."""
        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 302

        mock_http = MagicMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=mock_response)

        client = GitHubAppClient(
            app_id="12345",
            private_key_pem="fake-key",
            http_client=mock_http,
        )
        # Bypass JWT minting by mocking get_installation_token
        client.get_installation_token = AsyncMock(return_value="fake-token")

        with caplog.at_level("WARNING", logger="src.admin.connections.github_client"):
            result = await client.check_org_membership(
                installation_id=144637178,
                org_login="awslabs",
                username="testuser",
            )

        assert result is False
        assert "Organization members: read" in caplog.text
        assert "302" in caplog.text

    async def test_403_logs_warning(self, caplog):
        """403 forbidden (memberships endpoint without permission) logs a warning."""
        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 403

        mock_http = MagicMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=mock_response)

        client = GitHubAppClient(
            app_id="12345",
            private_key_pem="fake-key",
            http_client=mock_http,
        )
        client.get_installation_token = AsyncMock(return_value="fake-token")

        with caplog.at_level("WARNING", logger="src.admin.connections.github_client"):
            result = await client.check_org_membership(
                installation_id=144637256,
                org_login="acme-hackathon",
                username="testuser",
            )

        assert result is False
        assert "Organization members: read" in caplog.text
        assert "403" in caplog.text

    async def test_204_no_warning(self, caplog):
        """204 (member confirmed) does not log any warning."""
        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 204

        mock_http = MagicMock(spec=httpx.AsyncClient)
        mock_http.get = AsyncMock(return_value=mock_response)

        client = GitHubAppClient(
            app_id="12345",
            private_key_pem="fake-key",
            http_client=mock_http,
        )
        client.get_installation_token = AsyncMock(return_value="fake-token")

        with caplog.at_level("WARNING", logger="src.admin.connections.github_client"):
            result = await client.check_org_membership(
                installation_id=144637178,
                org_login="awslabs",
                username="testuser",
            )

        assert result is True
        assert "Organization members: read" not in caplog.text
