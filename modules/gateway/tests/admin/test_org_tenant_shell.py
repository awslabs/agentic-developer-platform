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
from src.shared.models.onboarding import Tenant, TenantMembership
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
    """Install routing into a PRE-EXISTING tenant (Issue #2952, amended by #4072).

    Reframed for Issue #4072 (#5, CRITICAL). This class previously contained

        test_org_install_routes_to_org_tenant

    which pre-created ``acme-corp`` and asserted that an install by a caller from
    ``caller-org-001`` — a caller holding NO membership in ``acme-corp`` — landed
    in ``acme-corp`` anyway. That test did not merely fail to catch the
    vulnerability; it encoded the vulnerability as the intended contract and
    would have failed the fix. Per #4068's C3 it is inverted here rather than
    silently edited, and the rationale is recorded in place:

    The install callback is deliberately unauthenticated (GitHub redirects the
    browser to it), so its only authenticator is the nonce, which binds the
    *caller*. The target tenant, by contrast, was re-derived from the
    caller-supplied ``installation_id`` → account → ``github_org_id`` chain. An
    attacker who installs their own GitHub App on an account whose numeric id
    matches a victim tenant's ``github_org_id`` therefore had every downstream
    write — membership row, ``org_admin`` grant, active-tenant switch, tenant
    secret seed, DynamoDB identity-index row — land in the VICTIM's tenant.

    Human decision D1 chose option (b): keep #2952's org-tenant routing, because
    a real GitHub org install should land in the org's shared workspace so
    co-workers share it, but make it conditional on the caller already holding
    membership in that tenant. So the property under test splits in two, and both
    halves are pinned below:

    * caller WITHOUT standing in the target tenant  -> denied, nothing written
      (``test_org_install_denied_when_caller_not_member_of_target``)
    * caller WITH standing in the target tenant     -> still routes to the org
      tenant, exactly as #2952 intended
      (``test_org_install_routes_to_org_tenant_for_member``)

    The second test is what remains of the original: same fixtures, same
    assertions, plus the membership row that makes the routing legitimate.
    """

    async def test_org_install_denied_when_caller_not_member_of_target(self, db_session: AsyncSession, caller_org, caller_user, _mock_env):
        """#4072 (#5): install must NOT be routed into a tenant the caller is not in."""
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
        ) as mock_seed:
            with pytest.raises(PermissionError):
                await install_callback(
                    installation_id=124731131,
                    setup_action="install",
                    state="test-jti-001",
                    db=db_session,
                    github_client=gh,
                )

        # Assert the OUTCOME, not the plumbing: no routing row, no membership,
        # no tenant secret, no identity-index row may exist for the victim.
        mapping = (
            await db_session.execute(
                select(ChannelTenantMap).where(
                    ChannelTenantMap.provider == "github",
                    ChannelTenantMap.provider_scope_id == "98765432",
                )
            )
        ).scalar_one_or_none()
        assert mapping is None

        membership = (await db_session.execute(select(TenantMembership).where(TenantMembership.tenant_id == "acme-corp"))).scalar_one_or_none()
        assert membership is None

        mock_seed.assert_not_awaited()
        _mock_env.assert_not_awaited()

    async def test_org_install_routes_to_org_tenant_for_member(self, db_session: AsyncSession, caller_org, caller_user, _mock_env):
        """#2952 preserved: a MEMBER of the org tenant still routes there, not to their own."""
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
        # The standing that makes this routing legitimate. Deliberately
        # is_active=False: is_active is per-user session state flipped by
        # switch_tenant, so membership alone must be sufficient (see
        # _caller_has_standing_in_tenant).
        db_session.add(
            TenantMembership(
                user_id=caller_user.id,
                tenant_id="acme-corp",
                role="member",
                is_active=False,
            )
        )
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
        """Issue #2952 (E): DDB write receives the org tenant id, not the installer's.

        Issue #4072 (#5): the pre-created target tenant now needs a membership row
        for the caller. Without it the install is refused before any DDB write, so
        the original form of this test asserted the identity-index row landing in a
        tenant the caller had no standing in — the #5 primitive. The property being
        pinned is unchanged (DDB gets the *resolved* tenant, not the installer's
        home tenant); only the setup is made legitimate.
        """
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
        db_session.add(
            TenantMembership(
                user_id=caller_user.id,
                tenant_id="target-org",
                role="member",
                is_active=False,
            )
        )
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


# ---------------------------------------------------------------------------
# created_via provenance (Issue #2724 slice B)
# ---------------------------------------------------------------------------


class TestCreatedViaProvenance:
    """Which path created an org row, recorded on the row itself.

    Issue #2724: the webhook's auto-register gate cannot key on "does a tenant
    row exist" because that signal is attacker-creatable — the *unauthenticated*
    no-nonce install callback creates an org shell itself when a stranger clicks
    Install on the public App. So the gate keys on provenance instead, and every
    creating path must stamp it truthfully:

      * ``operator``           — pre-existing rows (migration 025 server_default)
      * ``register_flow``      — an authenticated ADP flow (nonce-validated)
      * ``install_autocreate`` — the unauthenticated no-nonce install callback

    Only the first two are trusted by the gate.
    """

    async def test_default_is_register_flow(self, db_session: AsyncSession):
        """The default is the trusted value, so only the untrusted door must opt in.

        Deliberate: every caller of _upsert_org_tenant_shell except the no-nonce
        path is nonce-authenticated. A new authenticated caller added later
        inherits the correct stamp; a new *unauthenticated* caller is a
        security-relevant change that should be a conscious argument.
        """
        await _upsert_org_tenant_shell(
            owner_login="Acme-Corp",
            github_org_id="98765432",
            github_app_id="12345",
            db=db_session,
        )

        org = await db_session.get(Organization, "acme-corp")
        assert org.created_via == "register_flow"

    async def test_explicit_install_autocreate_is_stamped(self, db_session: AsyncSession):
        await _upsert_org_tenant_shell(
            owner_login="Public-Org",
            github_org_id="44444444",
            github_app_id="",
            db=db_session,
            created_via="install_autocreate",
        )

        org = await db_session.get(Organization, "public-org")
        assert org.created_via == "install_autocreate"

    async def test_idempotent_upsert_does_not_launder_provenance(self, db_session: AsyncSession):
        """An install_autocreate shell is NOT upgraded by a later trusted call.

        The laundering attack this closes: create the shell via the
        unauthenticated door, then get any authenticated path to touch the same
        org and inherit ``register_flow``. Provenance is create-only.
        """
        await _upsert_org_tenant_shell(
            owner_login="Public-Org",
            github_org_id="44444444",
            github_app_id="",
            db=db_session,
            created_via="install_autocreate",
        )
        await _upsert_org_tenant_shell(
            owner_login="Public-Org",
            github_org_id="44444444",
            github_app_id="99999",
            db=db_session,
            created_via="register_flow",
        )

        org = await db_session.get(Organization, "public-org")
        assert org.created_via == "install_autocreate"
        # The idempotent branch still did its real job.
        assert org.github_app_id == "99999"

    async def test_operator_row_is_not_downgraded(self, db_session: AsyncSession):
        """The converse: a real tenant that later takes a public-App install stays trusted.

        Without this, the no-nonce path would demote existing customers to
        untrusted the first time someone re-installed the App, and the gate would
        start denying live tenants.
        """
        org = Organization(
            id="acme-corp",
            name="Acme-Corp",
            aws_accounts=[],
            role_mappings={},
            settings={},
            created_via="operator",
        )
        db_session.add(org)
        await db_session.commit()

        await _upsert_org_tenant_shell(
            owner_login="Acme-Corp",
            github_org_id="98765432",
            github_app_id="12345",
            db=db_session,
            created_via="install_autocreate",
        )

        refreshed = await db_session.get(Organization, "acme-corp")
        assert refreshed.created_via == "operator"

    async def test_no_nonce_install_stamps_install_autocreate(self, db_session: AsyncSession, _mock_env):
        """End-to-end through the untrusted door: THE case this issue is about.

        Nothing authenticated this caller — no nonce, no session — so the org row
        it creates must be marked self-created.
        """
        gh = _mock_github_client(account_login="attacker-org", account_github_id=44444444)

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
                    state="",  # no nonce
                    db=db_session,
                    github_client=gh,
                )

        assert result["no_nonce"] is True
        org = await db_session.get(Organization, "attacker-org")
        assert org.created_via == "install_autocreate"

    async def test_nonce_install_stamps_register_flow(self, db_session: AsyncSession, caller_org, caller_user, _mock_env):
        """The authenticated install-callback path stays trusted (no over-tightening)."""
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
        org = await db_session.get(Organization, "new-org")
        assert org.created_via == "register_flow"

    @patch("src.admin.connections.service._store_app_credentials", new_callable=AsyncMock)
    @patch("src.admin.connections.service.get_github_app_provider")
    @patch("src.admin.connections.service._invalidate_login_enabled_cache")
    @patch("src.admin.connections.service.httpx.AsyncClient")
    async def test_register_app_callback_stamps_register_flow(
        self,
        mock_httpx_cls,
        mock_invalidate,
        mock_provider,
        mock_store,
        db_session: AsyncSession,
    ):
        """App registration is operator-driven and nonce-authenticated → trusted."""
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
            "owner": {"type": "Organization", "login": "Acme-Corp", "id": 98765432},
        }
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_httpx_cls.return_value = mock_client

        jti = "register-jti-prov"
        now = datetime.now(UTC)
        from src.admin.connections.service import _PROVIDER_GITHUB_APP_REGISTER

        db_session.add(
            MagicLinkNonce(
                jti=jti,
                provider=_PROVIDER_GITHUB_APP_REGISTER,
                provider_user_id="sub-abc",
                channel_context=None,
                target_user_id="user-001",
                expires_at=now + timedelta(minutes=15),
                consumed_at=None,
            )
        )
        await db_session.commit()

        await register_app_callback(code="test-code", state=jti, db=db_session)

        org = await db_session.get(Organization, "acme-corp")
        assert org.created_via == "register_flow"

    async def test_model_default_is_operator(self, db_session: AsyncSession):
        """A row created without the column set defaults to operator.

        This is the same grandfathering the migration's ``server_default`` gives
        every pre-existing deployment: rows that predate provenance are treated
        as operator-onboarded, so the gate does not evict live tenants.
        """
        org = Organization(
            id="legacy-org",
            name="Legacy Org",
            aws_accounts=[],
            role_mappings={},
            settings={},
        )
        db_session.add(org)
        await db_session.commit()

        refreshed = await db_session.get(Organization, "legacy-org")
        assert refreshed.created_via == "operator"


# ---------------------------------------------------------------------------
# The real bypass chain (Issue #2724 slice B, review finding)
# ---------------------------------------------------------------------------


class TestNoNonceInstallPromotionGate:
    """The gateway's own unauthenticated door must not promote a self-created shell.

    Stamping ``install_autocreate`` is only half a gate: the *same* request that
    creates the shell then performs both promoting side effects itself —

      1. ``seed_tenant_github_app_secret`` copies the PLATFORM App's private key
         into ``adp/<env>/tenants/<org>/github-app``, and
      2. ``_write_installation_identity_index`` writes the routable
         installation → tenant row the webhook resolver reads.

    Neither involves the webhook Lambda, so the Lambda-side gate and its
    ``authoritative`` flag are never consulted on this path. That made the
    primary attack path — stranger clicks Install on the public App, GitHub
    redirects their browser to the unauthenticated callback — immune to a
    perfect Lambda-side fix.

    These tests walk the real chain through ``install_callback`` rather than
    unit-testing the guard, because the guard being correct in isolation is
    exactly what the original slice B already had.
    """

    async def test_self_created_shell_is_not_promoted(self, db_session: AsyncSession, _mock_env):
        """THE bypass: unauthenticated install of an unknown org promotes nothing.

        The install itself still succeeds and the shell is still created — that is
        what keeps the install UI working and preserves the provenance signal the
        webhook gate reads. What must NOT happen is the platform vouching for the
        org by handing it credentials and a routable identity.
        """
        gh = _mock_github_client(account_login="attacker-org", account_github_id=44444444)

        with patch(
            "src.admin.connections.tenant_secret.seed_tenant_github_app_secret",
            new_callable=AsyncMock,
        ) as mock_seed:
            with patch(
                "src.admin.connections.service._get_github_app_credentials",
                return_value=("12345", "fake-pem"),
            ):
                result = await install_callback(
                    installation_id=666,
                    setup_action="install",
                    state="",  # no nonce — nothing authenticated this caller
                    db=db_session,
                    github_client=gh,
                )

        # The install flow still succeeds (no user-visible breakage).
        assert result["success"] is True
        assert result["no_nonce"] is True

        # The shell exists and is marked untrusted.
        org = await db_session.get(Organization, "attacker-org")
        assert org is not None
        assert org.created_via == "install_autocreate"

        # ...but the platform App private key was NOT copied for them.
        mock_seed.assert_not_called()
        # ...and no routable webhook identity was written. This is the row that
        # made review findings 2 and 3 reachable: it is what let the attacker
        # manufacture the "a DDB row exists, therefore Postgres owns it,
        # therefore trust it" premise on the Lambda side.
        _mock_env.assert_not_called()

    async def test_operator_org_taking_public_install_is_still_promoted(self, db_session: AsyncSession, _mock_env):
        """No over-tightening: a real tenant installing the public App is unaffected."""
        org = Organization(
            id="acme-corp",
            name="acme-corp",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_org_id="98765432",
            created_via="operator",
        )
        db_session.add(org)
        await db_session.commit()

        gh = _mock_github_client(account_login="acme-corp", account_github_id=98765432)

        with patch(
            "src.admin.connections.tenant_secret.seed_tenant_github_app_secret",
            new_callable=AsyncMock,
        ) as mock_seed:
            with patch(
                "src.admin.connections.service._get_github_app_credentials",
                return_value=("12345", "fake-pem"),
            ):
                result = await install_callback(
                    installation_id=888,
                    setup_action="install",
                    state="",
                    db=db_session,
                    github_client=gh,
                )

        assert result["success"] is True
        mock_seed.assert_awaited_once_with("acme-corp", 888)
        _mock_env.assert_called_once_with(installation_id=888, org_id="acme-corp")

    async def test_absent_provenance_fails_open(self, db_session: AsyncSession, _mock_env):
        """Unknown is not untrusted — an unrecognised value must not brick onboarding.

        Mirrors the Lambda gate's ``provenance_unavailable`` fail-open. Migration
        025's ``server_default`` means the only way to see an unrecognised value
        is a row written by code newer than this reader; denying on it would
        reject legitimate installs, the top row of this issue's blast-radius
        table.
        """
        org = Organization(
            id="legacy-org",
            name="legacy-org",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_org_id="77777777",
            created_via="some_future_value",
        )
        db_session.add(org)
        await db_session.commit()

        gh = _mock_github_client(account_login="legacy-org", account_github_id=77777777)

        with patch(
            "src.admin.connections.tenant_secret.seed_tenant_github_app_secret",
            new_callable=AsyncMock,
        ) as mock_seed:
            with patch(
                "src.admin.connections.service._get_github_app_credentials",
                return_value=("12345", "fake-pem"),
            ):
                await install_callback(
                    installation_id=999,
                    setup_action="install",
                    state="",
                    db=db_session,
                    github_client=gh,
                )

        mock_seed.assert_awaited_once_with("legacy-org", 999)
        _mock_env.assert_called_once_with(installation_id=999, org_id="legacy-org")

    async def test_reinstall_on_existing_shell_is_still_not_promoted(self, db_session: AsyncSession, _mock_env):
        """Uninstall/reinstall does not launder the shell into a promotion.

        The second install takes the ``org_by_github_id is not None`` branch, so
        the guard must read the EXISTING row's provenance rather than assume that
        "we did not create it in this request" means trusted.
        """
        org = Organization(
            id="attacker-org",
            name="attacker-org",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_org_id="44444444",
            created_via="install_autocreate",
        )
        db_session.add(org)
        await db_session.commit()

        gh = _mock_github_client(account_login="attacker-org", account_github_id=44444444)

        with patch(
            "src.admin.connections.tenant_secret.seed_tenant_github_app_secret",
            new_callable=AsyncMock,
        ) as mock_seed:
            with patch(
                "src.admin.connections.service._get_github_app_credentials",
                return_value=("12345", "fake-pem"),
            ):
                result = await install_callback(
                    installation_id=1234,
                    setup_action="install",
                    state="",
                    db=db_session,
                    github_client=gh,
                )

        assert result["success"] is True
        mock_seed.assert_not_called()
        _mock_env.assert_not_called()

    async def test_open_onboarding_deployment_can_still_promote(self, db_session: AsyncSession, _mock_env, monkeypatch):
        """A deliberately-open deployment restores promotion via the webhook gate.

        The gateway's refusal is unconditional by design: promotion of an
        ``install_autocreate`` shell is the webhook Lambda's single trust decision
        behind the single ``ORG_TENANT_AUTO_CREATE`` flag (#2724's BINDING
        one-flag rule), and the Lambda's own gate allows it when that flag is on.
        What this test pins is that the gateway's refusal is not *destructive* of
        that path: ``github_installation_ids`` is still populated, so
        ``resolve-installation`` answers 200 with ``install_autocreate`` and the
        Lambda can make the call. Withholding it would make the Lambda see an
        authoritative ``not_found`` and deny for the wrong reason.
        """
        gh = _mock_github_client(account_login="hackathon-org", account_github_id=66666666)

        with patch(
            "src.admin.connections.tenant_secret.seed_tenant_github_app_secret",
            new_callable=AsyncMock,
        ):
            with patch(
                "src.admin.connections.service._get_github_app_credentials",
                return_value=("12345", "fake-pem"),
            ):
                await install_callback(
                    installation_id=4321,
                    setup_action="install",
                    state="",
                    db=db_session,
                    github_client=gh,
                )

        org = await db_session.get(Organization, "hackathon-org")
        assert org.created_via == "install_autocreate"
        # The provenance signal the webhook gate reads is intact.
        assert "4321" in [str(i) for i in org.github_installation_ids]

    async def test_personal_install_promotes_nothing_either_way(self, db_session: AsyncSession, _mock_env):
        """Regression: a personal (non-Organization) install resolves no org at all.

        No org row resolves, so the promotion decision is never reached — behaviour
        is unchanged from before the gate.
        """
        gh = _mock_github_client(
            account_login="some-user",
            account_type="User",
            account_github_id=12121212,
        )

        with patch(
            "src.admin.connections.tenant_secret.seed_tenant_github_app_secret",
            new_callable=AsyncMock,
        ) as mock_seed:
            with patch(
                "src.admin.connections.service._get_github_app_credentials",
                return_value=("12345", "fake-pem"),
            ):
                result = await install_callback(
                    installation_id=2468,
                    setup_action="install",
                    state="",
                    db=db_session,
                    github_client=gh,
                )

        assert result["no_nonce"] is True
        mock_seed.assert_not_called()
        _mock_env.assert_not_called()
