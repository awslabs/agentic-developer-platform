"""Unit tests for install-callback tenant membership creation.

Issue #3035: When a user installs the GitHub App on an org, the install
callback must create a tenant_membership row for the installing user so
they can see the workspace in the multi-tenant Connections view.
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
    install_callback,
)
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import MagicLinkNonce

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


# ---------------------------------------------------------------------------
# Tests: install callback creates membership for org installs
# ---------------------------------------------------------------------------


class TestInstallCallbackMembership:
    """Issue #3035: install_callback creates a tenant_membership for the
    installing user on org-type installs."""

    async def test_org_install_creates_membership(self, db_session: AsyncSession, org_in_db):
        """Org install with authenticated user → membership row created with
        role=member, joined_via=app_install."""
        await _seed_user_and_nonce(db_session)
        gh = _mock_github_client()

        result = await install_callback(
            installation_id=124731131,
            setup_action="install",
            state="membership-jti",
            db=db_session,
            github_client=gh,
        )

        assert result["success"] is True

        # Verify membership was created
        stmt = select(TenantMembership).where(
            TenantMembership.user_id == "user-installer-001",
            TenantMembership.tenant_id == "org-test-001",
        )
        membership = (await db_session.execute(stmt)).scalar_one_or_none()
        assert membership is not None
        assert membership.role == "member"
        assert membership.joined_via == "app_install"
        assert membership.github_org_id == "sophos-test"

    async def test_first_membership_becomes_active(self, db_session: AsyncSession, org_in_db):
        """When the user has no existing memberships, the new one is set active
        (first-membership-active rule)."""
        await _seed_user_and_nonce(db_session)
        gh = _mock_github_client()

        await install_callback(
            installation_id=124731131,
            setup_action="install",
            state="membership-jti",
            db=db_session,
            github_client=gh,
        )

        stmt = select(TenantMembership).where(
            TenantMembership.user_id == "user-installer-001",
        )
        membership = (await db_session.execute(stmt)).scalar_one()
        assert membership.is_active is True

    async def test_reinstall_does_not_duplicate(self, db_session: AsyncSession, org_in_db):
        """Reinstall (membership already exists) → no duplicate, no error."""
        user, _ = await _seed_user_and_nonce(db_session, jti="first-jti")
        gh = _mock_github_client()

        # First install
        await install_callback(
            installation_id=124731131,
            setup_action="install",
            state="first-jti",
            db=db_session,
            github_client=gh,
        )

        # Seed a second nonce for the reinstall
        nonce2 = MagicLinkNonce(
            jti="second-jti",
            provider=_PROVIDER_GITHUB_INSTALL,
            provider_user_id="sub-installer",
            channel_context=None,
            target_user_id="user-installer-001",
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
            consumed_at=None,
        )
        db_session.add(nonce2)
        await db_session.commit()

        # Second install (reinstall) — should not raise or duplicate
        result = await install_callback(
            installation_id=124731131,
            setup_action="install",
            state="second-jti",
            db=db_session,
            github_client=gh,
        )

        assert result["success"] is True

        # Verify only one membership exists
        stmt = select(TenantMembership).where(
            TenantMembership.user_id == "user-installer-001",
            TenantMembership.tenant_id == "org-test-001",
        )
        memberships = (await db_session.execute(stmt)).scalars().all()
        assert len(memberships) == 1

    async def test_existing_active_membership_untouched(self, db_session: AsyncSession, org_in_db):
        """User with existing active membership → is_active NOT modified on
        the existing row."""
        user, _ = await _seed_user_and_nonce(db_session)

        # Create a second org for the existing membership
        other_org = Organization(
            id="org-other-active",
            name="Other Active Org",
            aws_accounts=[],
            role_mappings={},
            settings={},
        )
        db_session.add(other_org)
        await db_session.commit()

        # Pre-existing ACTIVE membership to a different tenant
        existing_membership = TenantMembership(
            user_id="user-installer-001",
            tenant_id="org-other-active",
            role="org_admin",
            is_active=True,
            joined_via="org_membership",
            github_org_id="other-org",
        )
        db_session.add(existing_membership)
        await db_session.commit()

        gh = _mock_github_client()
        result = await install_callback(
            installation_id=124731131,
            setup_action="install",
            state="membership-jti",
            db=db_session,
            github_client=gh,
        )

        assert result["success"] is True

        # The new membership should NOT be active (user already has one)
        stmt = select(TenantMembership).where(
            TenantMembership.user_id == "user-installer-001",
            TenantMembership.tenant_id == "org-test-001",
        )
        new_membership = (await db_session.execute(stmt)).scalar_one()
        assert new_membership.is_active is False
        assert new_membership.role == "member"
        assert new_membership.joined_via == "app_install"

        # The existing membership's is_active is UNTOUCHED
        await db_session.refresh(existing_membership)
        assert existing_membership.is_active is True

    async def test_personal_install_no_membership(self, db_session: AsyncSession, org_in_db):
        """Personal-account install → no membership row created."""
        await _seed_user_and_nonce(db_session)
        gh = _mock_github_client(account_type="User", account_login="alice")

        result = await install_callback(
            installation_id=999,
            setup_action="install",
            state="membership-jti",
            db=db_session,
            github_client=gh,
        )

        assert result["success"] is True
        assert result["account_type"] == "User"

        # No membership row should exist
        stmt = select(TenantMembership).where(
            TenantMembership.user_id == "user-installer-001",
        )
        memberships = (await db_session.execute(stmt)).scalars().all()
        assert len(memberships) == 0

    async def test_cognito_sub_user_resolution(self, db_session: AsyncSession, org_in_db):
        """Regression test for #3021 bug class: the user is resolved from the
        nonce (target_user_id → users.id), NOT from the ID token. The fixture
        uses an access-token-shaped cognito_sub (different from users.id)."""
        # The user's cognito_sub ≠ users.id — this is the standard case
        await _seed_user_and_nonce(
            db_session,
            user_id="pg-uuid-001",
            cognito_sub="cognito-sub-different-from-pg-id",
        )
        gh = _mock_github_client()

        result = await install_callback(
            installation_id=124731131,
            setup_action="install",
            state="membership-jti",
            db=db_session,
            github_client=gh,
        )

        assert result["success"] is True

        # Membership uses the Postgres users.id, not the cognito_sub
        stmt = select(TenantMembership).where(
            TenantMembership.user_id == "pg-uuid-001",
            TenantMembership.tenant_id == "org-test-001",
        )
        membership = (await db_session.execute(stmt)).scalar_one_or_none()
        assert membership is not None
        assert membership.role == "member"


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
                org_login="sophos-hackathon",
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
