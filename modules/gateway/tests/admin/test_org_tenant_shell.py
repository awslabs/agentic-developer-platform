"""Legacy organization-shell helper compatibility and App registration tests.

Registration no longer invokes shell creation. Selected-organization and public
callback behavior is covered by connections/test_setup_identity_binding.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.connections.github_client import GitHubAppClient
from src.admin.connections.service import (
    _build_app_manifest,
    _slugify_org_id,
    _upsert_org_tenant_shell,
    register_app_callback,
)
from src.shared.models.base import Base
from src.shared.models.onboarding import Tenant
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import MagicLinkNonce
from tests.admin import install_setup_fixtures as setup_fixtures
from tests.admin.install_setup_fixtures import (
    bind_real_org_control,
    issue_install_nonce,
    issue_register_nonce,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

offline_setup_boundaries = setup_fixtures.offline_setup_boundaries

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch, offline_setup_boundaries):
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
            # Issue #5664 moved the "an App is already registered" guard into
            # register_app_callback, before any secret write. It builds its own
            # boto3 client from ambient config, so with live AWS credentials
            # present (dev box, or a CI runner with a role attached) it reads the
            # REAL deployment's App id and refuses — making these success-path
            # tests pass or fail depending on the machine. Guard behaviour itself
            # is covered in tests/admin/test_register_app_callback_authority.py.
            with patch(
                "src.admin.connections.service._check_existing_app_secret",
                return_value=None,
            ):
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
    # These human-routing cases model an unavailable optional bot lookup.
    client.get_bot_user = AsyncMock(return_value={})
    return bind_real_org_control(client)


async def _write_nonce(
    db: AsyncSession,
    *,
    jti: str = "test-jti-001",
    target_user_id: str = "user-001",
) -> MagicLinkNonce:
    user = await db.get(User, target_user_id)
    return await issue_install_nonce(db, user, jti=jti)


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


async def _seed_register_initiator(db: AsyncSession, *, user_id: str = "user-001") -> None:
    """Seed the platform-admin who starts a GitHub App registration.

    Issue #5664: register_app_callback re-derives platform-admin authority from the
    `users` row recorded on the state nonce, because the callback is a tokenless
    browser redirect — there is no Authorization header, hence no `is_admin` claim
    to read. These tests assert org-tenant-shell creation on the SUCCESS path, so
    the initiator has to exist and hold the role. Refusal coverage lives in
    tests/admin/test_register_app_callback_authority.py.
    """
    org_id = "register-initiator-org"
    if await db.get(Organization, org_id) is None:
        db.add(
            Organization(
                id=org_id,
                name="Registrar Org",
                aws_accounts=[],
                role_mappings={},
                settings={},
            )
        )
        await db.commit()
    if await db.get(User, user_id) is None:
        db.add(
            User(
                id=user_id,
                org_id=org_id,
                team_id="team-registrar",
                email=f"{user_id}@registrar.local",
                cognito_sub="sub-abc",
                role="platform_admin",
            )
        )
        await db.commit()


class TestRegisterAppCallbackOrgTenant:
    async def _setup_nonce(self, db: AsyncSession, *, owner_type="org") -> str:
        """Write a register nonce and return the jti."""
        await _seed_register_initiator(db)
        user = await db.get(User, "user-001")
        return await issue_register_nonce(db, user, jti="register-jti-001", owner_type=owner_type)

    @patch("src.admin.connections.service._store_app_credentials", new_callable=AsyncMock)
    @patch("src.admin.connections.service.get_github_app_provider")
    @patch("src.admin.connections.service._invalidate_login_enabled_cache")
    @patch("src.admin.connections.service.httpx.AsyncClient")
    async def test_org_owner_does_not_create_adp_organization(
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
        assert redirect_url == "/settings/connections?github_app=registered"

        # Verify org-tenant shell was created
        org = await db_session.get(Organization, "acme-corp")
        assert org is None
        assert await db_session.get(Tenant, "acme-corp") is None

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

        jti = await self._setup_nonce(db_session, owner_type="user")
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


# ---------------------------------------------------------------------------
# No-nonce install path (Issue #2952 Rev 4 C)
# ---------------------------------------------------------------------------


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
