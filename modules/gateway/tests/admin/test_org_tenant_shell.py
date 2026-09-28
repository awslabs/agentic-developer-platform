"""Unit tests for org-tenant shell creation (Issue #2952).

Tests cover:
- Rule 1: register_app_callback creates Org+Tenant+Dept+Team when owner.type=Organization
- Idempotent re-register
- owner.type=User does NOT create org tenant
- Install-routing: org install resolves to org's tenant (not caller's)
- Install with no matching org tenant falls back to caller_org_id
- Install-time tenant upsert for unknown orgs (public Apps)
- No-nonce path (public-App install by non-ADP user)
- DDB write uses resolved org tenant
- Chained redirect (D9)
- App visibility toggle (D10)
- github_app_id populated on upsert
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
    _build_app_manifest,
    _slugify_org_id,
    _upsert_org_tenant_shell,
    install_callback,
    register_app_callback,
)
from src.shared.models.base import Base
from src.shared.models.onboarding import Tenant
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import ChannelTenantMap, MagicLinkNonce

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch):
    """Set up environment for tests."""
    monkeypatch.setenv("BG_GITHUB_APP_SLUG", "test-adp-agent")
    monkeypatch.setenv("ORG_TENANT_AUTO_CREATE", "true")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    # Block Secrets Manager and DDB access
    with patch(
        "src.admin.connections.github_app_provider.boto3.client",
        side_effect=RuntimeError("Secrets Manager blocked in unit tests"),
    ):
        with patch(
            "src.admin.connections.service._write_installation_identity_index",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_ddb:
            yield mock_ddb


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
async def caller_org(db_session: AsyncSession) -> Organization:
    """Create a caller's org (the installer's own tenant)."""
    org = Organization(
        id="caller-org-001",
        name="Caller Org",
        aws_accounts=[],
        role_mappings={},
        settings={},
    )
    db_session.add(org)
    await db_session.commit()
    return org


@pytest.fixture
async def caller_user(db_session: AsyncSession, caller_org: Organization) -> User:
    """Create a caller user in the caller's org."""
    user = User(
        id="user-001",
        org_id=caller_org.id,
        team_id="team-001",
        email="admin@caller.local",
        cognito_sub="sub-abc",
    )
    db_session.add(user)
    await db_session.commit()
    return user


def _mock_github_client(
    *,
    installation_id: int = 124731131,
    account_login: str = "acme-corp",
    account_type: str = "Organization",
    account_github_id: int = 98765432,
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
    client.list_installation_repository_names = AsyncMock(return_value=["acme-corp/repo-one", "acme-corp/repo-two"])
    return client


async def _write_nonce(
    db: AsyncSession,
    *,
    jti: str = "test-jti-001",
    target_user_id: str = "user-001",
) -> MagicLinkNonce:
    now = datetime.now(UTC)
    nonce = MagicLinkNonce(
        jti=jti,
        provider=_PROVIDER_GITHUB_INSTALL,
        provider_user_id="sub-abc",
        channel_context=None,
        target_user_id=target_user_id,
        expires_at=now + timedelta(minutes=15),
        consumed_at=None,
    )
    db.add(nonce)
    await db.commit()
    return nonce


# ---------------------------------------------------------------------------
# _slugify_org_id
# ---------------------------------------------------------------------------


class TestSlugifyOrgId:
    def test_basic_lowercase(self):
        assert _slugify_org_id("Acme-Corp") == "acme-corp"

    def test_special_chars(self):
        assert _slugify_org_id("My.Org_Name!") == "my-org-name"

    def test_strips_leading_trailing_hyphens(self):
        assert _slugify_org_id("--org--") == "org"

    def test_truncates_to_64(self):
        long_name = "a" * 100
        assert len(_slugify_org_id(long_name)) <= 64


# ---------------------------------------------------------------------------
# _upsert_org_tenant_shell
# ---------------------------------------------------------------------------


class TestUpsertOrgTenantShell:
    async def test_creates_org_tenant_dept_team(self, db_session: AsyncSession):
        result = await _upsert_org_tenant_shell(
            owner_login="Acme-Corp",
            github_org_id="98765432",
            github_app_id="12345",
            db=db_session,
        )

        assert result == "acme-corp"

        # Verify Organization
        org = await db_session.get(Organization, "acme-corp")
        assert org is not None
        assert org.name == "Acme-Corp"
        assert org.github_org_id == "98765432"
        assert org.github_app_id == "12345"

        # Verify Tenant
        tenant = await db_session.get(Tenant, "acme-corp")
        assert tenant is not None
        assert tenant.display_name == "Acme-Corp"

        # Verify Department
        dept = (await db_session.execute(select(Department).where(Department.org_id == "acme-corp"))).scalar_one()
        assert dept.name == "Default"

        # Verify Team
        team = (await db_session.execute(select(Team).where(Team.org_id == "acme-corp"))).scalar_one()
        assert team.name == "Default"
        assert team.department_id == dept.id

    async def test_idempotent_re_register(self, db_session: AsyncSession):
        """Re-registering the same org doesn't create duplicates."""
        result1 = await _upsert_org_tenant_shell(
            owner_login="Acme-Corp",
            github_org_id="98765432",
            github_app_id="12345",
            db=db_session,
        )
        result2 = await _upsert_org_tenant_shell(
            owner_login="Acme-Corp",
            github_org_id="98765432",
            github_app_id="12345",
            db=db_session,
        )

        assert result1 == result2 == "acme-corp"

        # Only one org row
        orgs = (await db_session.execute(select(Organization).where(Organization.id == "acme-corp"))).scalars().all()
        assert len(orgs) == 1

    async def test_updates_missing_ids_on_re_register(self, db_session: AsyncSession):
        """Re-register fills in github_org_id/github_app_id if previously unset."""
        # Create org without IDs
        org = Organization(
            id="acme-corp",
            name="Acme-Corp",
            aws_accounts=[],
            role_mappings={},
            settings={},
        )
        db_session.add(org)
        await db_session.commit()

        # Now upsert with IDs
        result = await _upsert_org_tenant_shell(
            owner_login="Acme-Corp",
            github_org_id="98765432",
            github_app_id="12345",
            db=db_session,
        )

        assert result == "acme-corp"
        refreshed = await db_session.get(Organization, "acme-corp")
        assert refreshed.github_org_id == "98765432"
        assert refreshed.github_app_id == "12345"

    async def test_does_not_overwrite_existing_ids(self, db_session: AsyncSession):
        """If IDs are already set, don't overwrite them."""
        org = Organization(
            id="acme-corp",
            name="Acme-Corp",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_org_id="original-id",
            github_app_id="original-app",
        )
        db_session.add(org)
        await db_session.commit()

        await _upsert_org_tenant_shell(
            owner_login="Acme-Corp",
            github_org_id="new-id",
            github_app_id="new-app",
            db=db_session,
        )

        refreshed = await db_session.get(Organization, "acme-corp")
        assert refreshed.github_org_id == "original-id"
        assert refreshed.github_app_id == "original-app"

    async def test_no_user_rows_created(self, db_session: AsyncSession):
        """Org-tenant shell does NOT create any User rows."""
        await _upsert_org_tenant_shell(
            owner_login="Acme-Corp",
            github_org_id="98765432",
            github_app_id="12345",
            db=db_session,
        )

        users = (await db_session.execute(select(User).where(User.org_id == "acme-corp"))).scalars().all()
        assert len(users) == 0


# ---------------------------------------------------------------------------
# register_app_callback — Rule 1 (org-tenant creation + chained redirect)
# ---------------------------------------------------------------------------


class TestRegisterAppCallbackOrgTenant:
    async def _setup_nonce(self, db: AsyncSession) -> str:
        """Write a register nonce and return the jti."""
        from src.admin.connections.service import _PROVIDER_GITHUB_APP_REGISTER

        jti = "register-jti-001"
        now = datetime.now(UTC)
        nonce = MagicLinkNonce(
            jti=jti,
            provider=_PROVIDER_GITHUB_APP_REGISTER,
            provider_user_id="sub-abc",
            channel_context=None,
            target_user_id="user-001",
            expires_at=now + timedelta(minutes=15),
            consumed_at=None,
        )
        db.add(nonce)
        await db.commit()
        return jti

    @patch("src.admin.connections.service._store_app_credentials", new_callable=AsyncMock)
    @patch("src.admin.connections.service.get_github_app_provider")
    @patch("src.admin.connections.service._invalidate_login_enabled_cache")
    @patch("src.admin.connections.service.httpx.AsyncClient")
    async def test_org_owner_creates_tenant_shell(
        self,
        mock_httpx_cls,
        mock_invalidate,
        mock_provider,
        mock_store,
        db_session: AsyncSession,
    ):
        """When owner.type=Organization, register creates Org+Tenant+Dept+Team."""
        mock_store.return_value = True
        mock_provider.return_value = MagicMock(invalidate=MagicMock())

        # Mock httpx response
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "id": 99999,
            "slug": "acme-corp-adp-agent-platform",
            "pem": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
            "client_id": "Iv1.abc123",
            "client_secret": "secret123",
            "webhook_secret": "whsec_xyz",
            "owner": {
                "type": "Organization",
                "login": "Acme-Corp",
                "id": 98765432,
            },
        }
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_httpx_cls.return_value = mock_client

        jti = await self._setup_nonce(db_session)
        redirect_url = await register_app_callback(code="test-code", state=jti, db=db_session)

        # Should redirect to GitHub install page (D9)
        assert redirect_url == "https://github.com/apps/acme-corp-adp-agent-platform/installations/new"

        # Verify org-tenant shell was created
        org = await db_session.get(Organization, "acme-corp")
        assert org is not None
        assert org.github_org_id == "98765432"
        assert org.github_app_id == "99999"

        tenant = await db_session.get(Tenant, "acme-corp")
        assert tenant is not None

    @patch("src.admin.connections.service._store_app_credentials", new_callable=AsyncMock)
    @patch("src.admin.connections.service.get_github_app_provider")
    @patch("src.admin.connections.service._invalidate_login_enabled_cache")
    @patch("src.admin.connections.service.httpx.AsyncClient")
    async def test_user_owner_does_not_create_tenant(
        self,
        mock_httpx_cls,
        mock_invalidate,
        mock_provider,
        mock_store,
        db_session: AsyncSession,
    ):
        """When owner.type=User, register does NOT create org tenant."""
        mock_store.return_value = True
        mock_provider.return_value = MagicMock(invalidate=MagicMock())

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "id": 88888,
            "slug": "alice-adp-agent-platform",
            "pem": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
            "client_id": "Iv1.def456",
            "client_secret": "secret456",
            "webhook_secret": "whsec_abc",
            "owner": {
                "type": "User",
                "login": "alice",
                "id": 12345,
            },
        }
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_httpx_cls.return_value = mock_client

        jti = await self._setup_nonce(db_session)
        await register_app_callback(code="test-code", state=jti, db=db_session)

        # No org created for personal account
        org = await db_session.get(Organization, "alice")
        assert org is None

    @patch("src.admin.connections.service._store_app_credentials", new_callable=AsyncMock)
    @patch("src.admin.connections.service.get_github_app_provider")
    @patch("src.admin.connections.service._invalidate_login_enabled_cache")
    @patch("src.admin.connections.service.httpx.AsyncClient")
    async def test_org_tenant_not_created_when_flag_off(
        self,
        mock_httpx_cls,
        mock_invalidate,
        mock_provider,
        mock_store,
        db_session: AsyncSession,
        monkeypatch,
    ):
        """Feature flag ORG_TENANT_AUTO_CREATE=false prevents tenant creation."""
        monkeypatch.setenv("ORG_TENANT_AUTO_CREATE", "false")
        mock_store.return_value = True
        mock_provider.return_value = MagicMock(invalidate=MagicMock())

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "id": 99999,
            "slug": "acme-corp-adp-agent-platform",
            "pem": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
            "client_id": "Iv1.abc123",
            "client_secret": "secret123",
            "webhook_secret": "whsec_xyz",
            "owner": {
                "type": "Organization",
                "login": "Acme-Corp",
                "id": 98765432,
            },
        }
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_httpx_cls.return_value = mock_client

        jti = await self._setup_nonce(db_session)
        await register_app_callback(code="test-code", state=jti, db=db_session)

        # No org created when flag is off
        org = await db_session.get(Organization, "acme-corp")
        assert org is None


# ---------------------------------------------------------------------------
# install_callback — install routing (Issue #2952)
# ---------------------------------------------------------------------------


class TestInstallCallbackOrgRouting:
    async def test_org_install_routes_to_org_tenant(self, db_session: AsyncSession, caller_org, caller_user, _mock_env):
        """Org install resolves to the org's tenant, not the caller's."""
        # Pre-create the org tenant with github_org_id
        target_org = Organization(
            id="acme-corp",
            name="Acme-Corp",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_org_id="98765432",
        )
        db_session.add(target_org)
        await db_session.commit()

        await _write_nonce(db_session)
        gh = _mock_github_client(account_github_id=98765432)

        with patch(
            "src.admin.connections.tenant_secret.seed_tenant_github_app_secret",
            new_callable=AsyncMock,
        ):
            result = await install_callback(
                installation_id=124731131,
                setup_action="install",
                state="test-jti-001",
                db=db_session,
                github_client=gh,
            )

        assert result["success"] is True

        # Verify install attached to acme-corp, NOT caller-org-001
        mapping = (
            await db_session.execute(
                select(ChannelTenantMap).where(
                    ChannelTenantMap.provider == "github",
                    ChannelTenantMap.provider_scope_id == "98765432",
                )
            )
        ).scalar_one()
        assert mapping.org_id == "acme-corp"

        # Verify DDB write used the resolved org tenant
        _mock_env.assert_called_with(
            installation_id=124731131,
            org_id="acme-corp",
        )

    async def test_org_install_falls_back_to_caller_when_no_match(self, db_session: AsyncSession, caller_org, caller_user, _mock_env):
        """When no org matches github_org_id and flag is off, falls back to caller."""
        # Set flag off so no upsert happens
        import os

        with patch.dict(os.environ, {"ORG_TENANT_AUTO_CREATE": "false"}):
            await _write_nonce(db_session)
            gh = _mock_github_client(account_github_id=11111111)

            with patch(
                "src.admin.connections.tenant_secret.seed_tenant_github_app_secret",
                new_callable=AsyncMock,
            ):
                result = await install_callback(
                    installation_id=999,
                    setup_action="install",
                    state="test-jti-001",
                    db=db_session,
                    github_client=gh,
                )

        assert result["success"] is True

        # Falls back to caller's org
        mapping = (
            await db_session.execute(
                select(ChannelTenantMap).where(
                    ChannelTenantMap.provider == "github",
                    ChannelTenantMap.provider_scope_id == "11111111",
                )
            )
        ).scalar_one()
        assert mapping.org_id == "caller-org-001"

    async def test_unknown_org_upserts_tenant_shell_on_install(self, db_session: AsyncSession, caller_org, caller_user, _mock_env):
        """Public-App install by unknown org creates the tenant shell."""
        await _write_nonce(db_session)
        gh = _mock_github_client(account_login="new-org", account_github_id=55555555)

        with patch(
            "src.admin.connections.tenant_secret.seed_tenant_github_app_secret",
            new_callable=AsyncMock,
        ):
            result = await install_callback(
                installation_id=777,
                setup_action="install",
                state="test-jti-001",
                db=db_session,
                github_client=gh,
            )

        assert result["success"] is True

        # Verify tenant shell was created
        org = await db_session.get(Organization, "new-org")
        assert org is not None
        assert org.github_org_id == "55555555"

        tenant = await db_session.get(Tenant, "new-org")
        assert tenant is not None

        # Install attached to the new org
        mapping = (
            await db_session.execute(
                select(ChannelTenantMap).where(
                    ChannelTenantMap.provider == "github",
                    ChannelTenantMap.provider_scope_id == "55555555",
                )
            )
        ).scalar_one()
        assert mapping.org_id == "new-org"

    async def test_personal_install_routes_to_caller(self, db_session: AsyncSession, caller_org, caller_user, _mock_env):
        """Personal (User) installs still route to the caller's tenant."""
        await _write_nonce(db_session)
        gh = _mock_github_client(
            account_type="User",
            account_login="alice",
            account_github_id=12345,
        )

        with patch(
            "src.admin.connections.tenant_secret.seed_tenant_github_app_secret",
            new_callable=AsyncMock,
        ):
            result = await install_callback(
                installation_id=888,
                setup_action="install",
                state="test-jti-001",
                db=db_session,
                github_client=gh,
            )

        assert result["success"] is True

        mapping = (
            await db_session.execute(
                select(ChannelTenantMap).where(
                    ChannelTenantMap.provider == "github",
                    ChannelTenantMap.provider_scope_id == "12345",
                )
            )
        ).scalar_one()
        # Personal installs go to caller's org
        assert mapping.org_id == "caller-org-001"

    async def test_ddb_write_uses_resolved_org_tenant(self, db_session: AsyncSession, caller_org, caller_user, _mock_env):
        """Issue #2952 (E): DDB write receives the org tenant id, not the installer's."""
        # Pre-create the org tenant
        target_org = Organization(
            id="target-org",
            name="Target-Org",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_org_id="77777777",
        )
        db_session.add(target_org)
        await db_session.commit()

        await _write_nonce(db_session)
        gh = _mock_github_client(account_login="Target-Org", account_github_id=77777777)

        with patch(
            "src.admin.connections.tenant_secret.seed_tenant_github_app_secret",
            new_callable=AsyncMock,
        ):
            await install_callback(
                installation_id=555,
                setup_action="install",
                state="test-jti-001",
                db=db_session,
                github_client=gh,
            )

        # The DDB mock was called with the resolved org tenant, not caller's
        _mock_env.assert_called_with(
            installation_id=555,
            org_id="target-org",
        )


# ---------------------------------------------------------------------------
# No-nonce install path (Issue #2952 Rev 4 C)
# ---------------------------------------------------------------------------


class TestNoNonceInstall:
    async def test_empty_state_creates_tenant_shell(self, db_session: AsyncSession, _mock_env):
        """Install with empty state creates tenant shell for the org."""
        gh = _mock_github_client(account_login="public-org", account_github_id=44444444)

        with patch(
            "src.admin.connections.tenant_secret.seed_tenant_github_app_secret",
            new_callable=AsyncMock,
        ):
            with patch(
                "src.admin.connections.service._get_github_app_credentials",
                return_value=("12345", "fake-pem"),
            ):
                result = await install_callback(
                    installation_id=666,
                    setup_action="install",
                    state="",  # Empty state = no-nonce path
                    db=db_session,
                    github_client=gh,
                )

        assert result["success"] is True
        assert result["no_nonce"] is True

        # Verify tenant shell created
        org = await db_session.get(Organization, "public-org")
        assert org is not None
        assert org.github_org_id == "44444444"

        tenant = await db_session.get(Tenant, "public-org")
        assert tenant is not None

    async def test_empty_state_no_user_rows_created(self, db_session: AsyncSession, _mock_env):
        """No-nonce path creates no User or UserIdentity rows."""
        gh = _mock_github_client(account_login="public-org", account_github_id=44444444)

        with patch(
            "src.admin.connections.tenant_secret.seed_tenant_github_app_secret",
            new_callable=AsyncMock,
        ):
            with patch(
                "src.admin.connections.service._get_github_app_credentials",
                return_value=("12345", "fake-pem"),
            ):
                await install_callback(
                    installation_id=666,
                    setup_action="install",
                    state="",
                    db=db_session,
                    github_client=gh,
                )

        users = (await db_session.execute(select(User).where(User.org_id == "public-org"))).scalars().all()
        assert len(users) == 0

    async def test_empty_state_existing_org_attaches(self, db_session: AsyncSession, _mock_env):
        """No-nonce install on existing org attaches to it without creating a new one."""
        # Pre-create the org
        org = Organization(
            id="existing-org",
            name="existing-org",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_org_id="33333333",
        )
        db_session.add(org)
        await db_session.commit()

        gh = _mock_github_client(account_login="existing-org", account_github_id=33333333)

        with patch(
            "src.admin.connections.tenant_secret.seed_tenant_github_app_secret",
            new_callable=AsyncMock,
        ):
            with patch(
                "src.admin.connections.service._get_github_app_credentials",
                return_value=("12345", "fake-pem"),
            ):
                result = await install_callback(
                    installation_id=777,
                    setup_action="install",
                    state="",
                    db=db_session,
                    github_client=gh,
                )

        assert result["success"] is True

        # Verify attached to existing org
        mapping = (
            await db_session.execute(
                select(ChannelTenantMap).where(
                    ChannelTenantMap.provider == "github",
                    ChannelTenantMap.provider_scope_id == "33333333",
                )
            )
        ).scalar_one()
        assert mapping.org_id == "existing-org"


# ---------------------------------------------------------------------------
# App visibility toggle (D10)
# ---------------------------------------------------------------------------


class TestAppVisibilityToggle:
    def test_manifest_default_private(self):
        """Default manifest has public=False."""
        manifest = _build_app_manifest(
            webhook_url="https://hook.example.com",
            callback_url="https://callback.example.com",
        )
        assert manifest["public"] is False

    def test_manifest_public_when_requested(self):
        """Manifest has public=True when public=True is passed."""
        manifest = _build_app_manifest(
            webhook_url="https://hook.example.com",
            callback_url="https://callback.example.com",
            public=True,
        )
        assert manifest["public"] is True

    def test_manifest_private_when_requested(self):
        """Explicit public=False."""
        manifest = _build_app_manifest(
            webhook_url="https://hook.example.com",
            callback_url="https://callback.example.com",
            public=False,
        )
        assert manifest["public"] is False
