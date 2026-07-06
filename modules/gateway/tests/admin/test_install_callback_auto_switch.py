"""Unit tests for install-callback auto-switch active tenant.

Issue #3072: After installing the GitHub App on an org, the installer's
active tenant is automatically switched to the installed org so they land
IN the workspace. The redirect includes `installed` + `switched_from` query
params so the frontend can show a confirmation banner.

Tests:
  - Org install → active membership flipped to the installed org.
  - Reinstall while org already active → no-op, no error.
  - Personal-account install → no switch.
  - No-nonce (public) install → no switch, no crash.
  - Redirect URL carries `installed` + `switched_from`.
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
    """Create a minimal org row required by FK constraints."""
    org = Organization(
        id="org-target-001",
        name="Target Org",
        aws_accounts=[],
        role_mappings={},
        settings={},
    )
    db_session.add(org)
    await db_session.commit()
    return org


@pytest.fixture
async def previous_org(db_session: AsyncSession) -> Organization:
    """Create a second org to be the 'previous' active workspace."""
    org = Organization(
        id="org-previous-001",
        name="Previous Workspace",
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
    account_login: str = "target-org",
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
            "created_at": "2026-07-01T10:00:00Z",
        }
    )
    client.delete_installation = AsyncMock(return_value=None)
    client.list_installation_repositories = AsyncMock(return_value=2)
    client.list_installation_repository_names = AsyncMock(return_value=["target-org/repo-one", "target-org/repo-two"])
    return client


async def _seed_user_and_nonce(
    db: AsyncSession,
    *,
    jti: str = "switch-jti",
    user_id: str = "user-installer-001",
    cognito_sub: str = "sub-installer",
    org_id: str = "org-target-001",
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
# Tests: auto-switch after org install
# ---------------------------------------------------------------------------


class TestInstallCallbackAutoSwitch:
    """Issue #3072: install_callback auto-switches the installer's active
    tenant to the newly-installed org."""

    async def test_org_install_switches_active_tenant(self, db_session: AsyncSession, org_in_db, previous_org):
        """Org install with a different previously-active workspace →
        active membership flipped to the installed org."""
        user, _ = await _seed_user_and_nonce(db_session)

        # Create a pre-existing ACTIVE membership to a different org
        existing_membership = TenantMembership(
            user_id="user-installer-001",
            tenant_id="org-previous-001",
            role="org_admin",
            is_active=True,
            joined_via="org_membership",
            github_org_id="previous-org",
        )
        db_session.add(existing_membership)
        await db_session.commit()

        gh = _mock_github_client()
        result = await install_callback(
            installation_id=124731131,
            setup_action="install",
            state="switch-jti",
            db=db_session,
            github_client=gh,
        )

        assert result["success"] is True
        assert result["switched_from"] == "org-previous-001"

        # Verify: target org is now active
        target_stmt = select(TenantMembership).where(
            TenantMembership.user_id == "user-installer-001",
            TenantMembership.tenant_id == "org-target-001",
        )
        target_membership = (await db_session.execute(target_stmt)).scalar_one()
        assert target_membership.is_active is True

        # Verify: previous org is no longer active
        prev_stmt = select(TenantMembership).where(
            TenantMembership.user_id == "user-installer-001",
            TenantMembership.tenant_id == "org-previous-001",
        )
        prev_membership = (await db_session.execute(prev_stmt)).scalar_one()
        assert prev_membership.is_active is False

        # Verify: exactly one active membership
        active_stmt = select(TenantMembership).where(
            TenantMembership.user_id == "user-installer-001",
            TenantMembership.is_active == True,  # noqa: E712
        )
        active_memberships = (await db_session.execute(active_stmt)).scalars().all()
        assert len(active_memberships) == 1
        assert active_memberships[0].tenant_id == "org-target-001"

    async def test_reinstall_while_already_active_is_noop(self, db_session: AsyncSession, org_in_db):
        """Reinstall when the org is already active → no-op, switched_from is None."""
        user, _ = await _seed_user_and_nonce(db_session, jti="first-jti")
        gh = _mock_github_client()

        # First install — creates membership + makes it active (first-membership rule)
        result1 = await install_callback(
            installation_id=124731131,
            setup_action="install",
            state="first-jti",
            db=db_session,
            github_client=gh,
        )
        assert result1["success"] is True

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

        # Reinstall — should be a no-op
        result2 = await install_callback(
            installation_id=124731131,
            setup_action="install",
            state="second-jti",
            db=db_session,
            github_client=gh,
        )

        assert result2["success"] is True
        # No switch occurred — target was already active
        assert result2["switched_from"] is None

    async def test_personal_install_no_switch(self, db_session: AsyncSession, org_in_db):
        """Personal-account install → no switch, switched_from is None."""
        await _seed_user_and_nonce(db_session)
        gh = _mock_github_client(account_type="User", account_login="alice")

        result = await install_callback(
            installation_id=999,
            setup_action="install",
            state="switch-jti",
            db=db_session,
            github_client=gh,
        )

        assert result["success"] is True
        assert result["account_type"] == "User"
        assert result["switched_from"] is None

        # No membership row should exist (personal installs don't create one)
        stmt = select(TenantMembership).where(
            TenantMembership.user_id == "user-installer-001",
        )
        memberships = (await db_session.execute(stmt)).scalars().all()
        assert len(memberships) == 0

    async def test_no_nonce_install_no_switch(self, db_session: AsyncSession, org_in_db):
        """No-nonce (public) install → no switch, no crash."""
        gh = _mock_github_client()

        result = await install_callback(
            installation_id=124731131,
            setup_action="install",
            state="",  # Empty state = no-nonce path
            db=db_session,
            github_client=gh,
        )

        # No-nonce path returns no_nonce=True, no switched_from
        assert result.get("no_nonce") is True
        assert result.get("switched_from") is None

    async def test_first_install_no_previous_active(self, db_session: AsyncSession, org_in_db):
        """First-ever install (no previous memberships) → active via
        first-membership rule; switched_from is None because there was
        nothing to switch FROM."""
        await _seed_user_and_nonce(db_session)
        gh = _mock_github_client()

        result = await install_callback(
            installation_id=124731131,
            setup_action="install",
            state="switch-jti",
            db=db_session,
            github_client=gh,
        )

        assert result["success"] is True
        # No switch because first-membership-active rule already made it active
        assert result["switched_from"] is None

        # Verify membership is active
        stmt = select(TenantMembership).where(
            TenantMembership.user_id == "user-installer-001",
            TenantMembership.tenant_id == "org-target-001",
        )
        membership = (await db_session.execute(stmt)).scalar_one()
        assert membership.is_active is True


# ---------------------------------------------------------------------------
# Tests: redirect URL carries switch params
# ---------------------------------------------------------------------------


class TestRedirectSuccessParams:
    """Issue #3072: _redirect_success passes installed + switched_from to the
    frontend redirect URL."""

    def test_redirect_with_switch_params(self):
        """When installed + switched_from are provided, URL includes them."""
        from src.admin.connections.routes import _redirect_success

        response = _redirect_success(
            123,
            installed="sophos-hackathon",
            switched_from="org-previous-001",
        )
        assert response.status_code == 302
        location = response.headers["location"]
        assert "installed=sophos-hackathon" in location
        assert "switched_from=org-previous-001" in location
        assert "success=1" in location

    def test_redirect_without_switch_params(self):
        """When no switch occurred, URL only has success + installation_id."""
        from src.admin.connections.routes import _redirect_success

        response = _redirect_success(456)
        assert response.status_code == 302
        location = response.headers["location"]
        assert "success=1" in location
        assert "installation_id=456" in location
        assert "installed" not in location
        assert "switched_from" not in location

    def test_redirect_with_none_params(self):
        """Explicit None for switch params → not included in URL."""
        from src.admin.connections.routes import _redirect_success

        response = _redirect_success(789, installed=None, switched_from=None)
        assert response.status_code == 302
        location = response.headers["location"]
        assert "installed" not in location
        assert "switched_from" not in location
