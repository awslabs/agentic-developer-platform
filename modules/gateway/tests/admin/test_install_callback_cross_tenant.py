"""Issue #4072 (#5, CRITICAL): the install callback must not route an install into
a tenant the caller has no standing in.

The vulnerability
-----------------
``install_callback`` is deliberately unauthenticated: GitHub redirects the user's
browser to it after they complete an installation, so there is no bearer token to
check. Its only authenticator is the one-time nonce minted at install-start, and
the nonce binds the **caller**.

The **target tenant**, however, was re-derived from data the caller supplies —
``installation_id`` → GitHub account → ``github_org_id`` → matching
``organizations`` row. Anyone who could make that chain land on a victim tenant
had every downstream write performed against the victim:

* a ``tenant_memberships`` row in the victim tenant, with ``role='org_admin'``
  (#4006 makes the installer an org admin by contract),
* their active tenant switched INTO the victim workspace,
* the victim tenant's GitHub App secret seeded,
* the victim's ``channel_tenant_map`` routing row rewritten,
* the victim's DynamoDB identity-index row rewritten.

The existing ``_attach_org_installation`` cross-tenant guard could not fire: it
compared against the tenant id that had *already* been overwritten with the
victim's.

The fix (decision D1, option (b))
---------------------------------
#2952's org-tenant routing is kept — a genuine GitHub org install SHOULD land in
the org's shared workspace so co-workers share it — but is made conditional on the
caller already having STANDING in that tenant: it is their own tenant, or they
already hold a ``tenant_memberships`` row in it. Standing is checked BEFORE any
write, so a refused install leaves the victim byte-identical.

Gate discipline (sub-EPIC #4068)
--------------------------------
Every test below asserts the OUTCOME in the victim's tenant — no membership row,
no ``org_admin`` grant, no active-tenant switch, no secret seed, no identity-index
row — not that a particular guard function ran. Deleting the guard makes these
fail; stubbing it out to a no-op also makes them fail. A "was the guard called"
assertion would pass in both cases, which is the #4046 failure mode this sub-EPIC
exists to reject.

The complementary "legitimate flows still work" cases live in
``test_org_tenant_shell.py`` (org member routes to the org tenant; first installer
of a brand-new org gets their membership), alongside the #2952 tests they amend.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.connections.github_client import GitHubAppClient
from src.admin.connections.service import _PROVIDER_GITHUB_INSTALL, install_callback
from src.shared.models.base import Base
from src.shared.models.onboarding import Tenant, TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import ChannelTenantMap, MagicLinkNonce

pytestmark = pytest.mark.asyncio

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

VICTIM_TENANT = "victim-corp"
VICTIM_GITHUB_ORG_ID = 98765432
ATTACKER_TENANT = "attacker-org"
INSTALL_ID = 4072001


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_aws(monkeypatch):
    """Block Secrets Manager and DynamoDB; expose the identity-index mock."""
    monkeypatch.setenv("BG_GITHUB_APP_SLUG", "test-adp-agent")
    monkeypatch.setenv("ORG_TENANT_AUTO_CREATE", "true")
    monkeypatch.setenv("ENVIRONMENT", "dev")
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
async def attacker(db_session: AsyncSession) -> User:
    """A legitimate self-service account: own tenant, own user, nothing more.

    This is the whole point of the CRITICAL rating — the attacker needs no special
    privilege anywhere. Signing up is enough.
    """
    org = Organization(
        id=ATTACKER_TENANT,
        name="Attacker Org",
        aws_accounts=[],
        role_mappings={},
        settings={},
    )
    db_session.add(org)
    await db_session.commit()

    user = User(
        id="attacker-user-001",
        org_id=ATTACKER_TENANT,
        team_id="team-001",
        email="attacker@example.test",
        cognito_sub="sub-attacker",
    )
    db_session.add(user)
    # The attacker's legitimate membership in their OWN tenant, active — so the
    # auto-switch assertions below distinguish "still in own tenant" from
    # "switched into the victim's".
    db_session.add(
        TenantMembership(
            user_id="attacker-user-001",
            tenant_id=ATTACKER_TENANT,
            role="org_admin",
            is_active=True,
        )
    )
    await db_session.commit()
    return user


@pytest.fixture
async def victim(db_session: AsyncSession) -> Organization:
    """A pre-existing victim tenant with a real GitHub org bound to it."""
    org = Organization(
        id=VICTIM_TENANT,
        name="Victim Corp",
        aws_accounts=[],
        role_mappings={},
        settings={},
        github_org_id=str(VICTIM_GITHUB_ORG_ID),
    )
    db_session.add(org)
    db_session.add(Tenant(id=VICTIM_TENANT, display_name="Victim Corp"))
    await db_session.commit()
    return org


def _github_client(*, account_login: str = "Victim-Corp", account_github_id: int = VICTIM_GITHUB_ORG_ID) -> MagicMock:
    client = MagicMock(spec=GitHubAppClient)
    client.get_installation = AsyncMock(
        return_value={
            "id": INSTALL_ID,
            "account": {"type": "Organization", "login": account_login, "id": account_github_id},
            "repository_selection": "selected",
            "created_at": "2026-08-01T10:00:00Z",
        }
    )
    client.list_installation_repository_names = AsyncMock(return_value=["victim-corp/private-repo"])
    return client


async def _write_nonce(db: AsyncSession, *, jti: str = "jti-4072") -> None:
    """The nonce the attacker legitimately obtained for their own session."""
    db.add(
        MagicLinkNonce(
            jti=jti,
            provider=_PROVIDER_GITHUB_INSTALL,
            provider_user_id="sub-attacker",
            channel_context=None,
            target_user_id="attacker-user-001",
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
            consumed_at=None,
        )
    )
    await db.commit()


# ---------------------------------------------------------------------------
# The takeover, blocked
# ---------------------------------------------------------------------------


class TestCrossTenantTakeoverRefused:
    async def test_install_into_foreign_tenant_is_refused(self, db_session: AsyncSession, attacker, victim, _no_aws):
        """The caller-supplied installation must not re-point the target tenant."""
        await _write_nonce(db_session)

        with patch("src.admin.connections.tenant_secret.seed_tenant_github_app_secret", new_callable=AsyncMock):
            with pytest.raises(PermissionError):
                await install_callback(
                    installation_id=INSTALL_ID,
                    setup_action="install",
                    state="jti-4072",
                    db=db_session,
                    github_client=_github_client(),
                )

    async def test_no_membership_is_created_in_the_victim_tenant(self, db_session: AsyncSession, attacker, victim, _no_aws):
        """The privilege-escalation payload: an org_admin row in someone else's tenant."""
        await _write_nonce(db_session)

        with patch("src.admin.connections.tenant_secret.seed_tenant_github_app_secret", new_callable=AsyncMock):
            with pytest.raises(PermissionError):
                await install_callback(
                    installation_id=INSTALL_ID,
                    setup_action="install",
                    state="jti-4072",
                    db=db_session,
                    github_client=_github_client(),
                )

        rows = list((await db_session.execute(select(TenantMembership).where(TenantMembership.tenant_id == VICTIM_TENANT))).scalars().all())
        assert rows == []

    async def test_attacker_is_not_switched_into_the_victim_workspace(self, db_session: AsyncSession, attacker, victim, _no_aws):
        """Active tenant must stay the attacker's own — no landing in the victim's UI."""
        await _write_nonce(db_session)

        with patch("src.admin.connections.tenant_secret.seed_tenant_github_app_secret", new_callable=AsyncMock):
            with pytest.raises(PermissionError):
                await install_callback(
                    installation_id=INSTALL_ID,
                    setup_action="install",
                    state="jti-4072",
                    db=db_session,
                    github_client=_github_client(),
                )

        active = list(
            (
                await db_session.execute(
                    select(TenantMembership).where(
                        TenantMembership.user_id == "attacker-user-001",
                        TenantMembership.is_active == True,  # noqa: E712
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [row.tenant_id for row in active] == [ATTACKER_TENANT]

    async def test_no_identity_index_row_is_written_for_the_victim(self, db_session: AsyncSession, attacker, victim, _no_aws):
        """The DDB row is what webhook-ingress reads to route events.

        Asserted separately from the Postgres rows because the identity index is a
        different store with a different failure mode: a guard that refused only
        after this write would already have redirected the victim's live webhook
        traffic to the attacker's tenant.
        """
        await _write_nonce(db_session)

        with patch("src.admin.connections.tenant_secret.seed_tenant_github_app_secret", new_callable=AsyncMock):
            with pytest.raises(PermissionError):
                await install_callback(
                    installation_id=INSTALL_ID,
                    setup_action="install",
                    state="jti-4072",
                    db=db_session,
                    github_client=_github_client(),
                )

        _no_aws.assert_not_awaited()

    async def test_no_tenant_secret_is_seeded_for_the_victim(self, db_session: AsyncSession, attacker, victim, _no_aws):
        """Seeding copies App credentials into the victim tenant's secret path."""
        await _write_nonce(db_session)

        with patch(
            "src.admin.connections.tenant_secret.seed_tenant_github_app_secret",
            new_callable=AsyncMock,
        ) as mock_seed:
            with pytest.raises(PermissionError):
                await install_callback(
                    installation_id=INSTALL_ID,
                    setup_action="install",
                    state="jti-4072",
                    db=db_session,
                    github_client=_github_client(),
                )

        mock_seed.assert_not_awaited()

    async def test_victim_channel_routing_is_untouched(self, db_session: AsyncSession, attacker, victim, _no_aws):
        """No routing row may be created for, or re-pointed at, the victim."""
        await _write_nonce(db_session)

        with patch("src.admin.connections.tenant_secret.seed_tenant_github_app_secret", new_callable=AsyncMock):
            with pytest.raises(PermissionError):
                await install_callback(
                    installation_id=INSTALL_ID,
                    setup_action="install",
                    state="jti-4072",
                    db=db_session,
                    github_client=_github_client(),
                )

        rows = list((await db_session.execute(select(ChannelTenantMap))).scalars().all())
        assert rows == []

    async def test_victim_installation_id_list_is_untouched(self, db_session: AsyncSession, attacker, victim, _no_aws):
        """``organizations.github_installation_ids`` drives tenant matching at login."""
        await _write_nonce(db_session)

        with patch("src.admin.connections.tenant_secret.seed_tenant_github_app_secret", new_callable=AsyncMock):
            with pytest.raises(PermissionError):
                await install_callback(
                    installation_id=INSTALL_ID,
                    setup_action="install",
                    state="jti-4072",
                    db=db_session,
                    github_client=_github_client(),
                )

        refreshed = (await db_session.execute(select(Organization).where(Organization.id == VICTIM_TENANT))).scalar_one()
        assert (refreshed.github_installation_ids or []) == []


class TestSlugCollisionBypassRefused:
    """The second door into a pre-existing tenant.

    ``_upsert_org_tenant_shell`` is idempotent BY SLUG, so when the GitHub account
    login slugifies onto an existing organization id it returns that existing
    tenant rather than creating one. That is the same re-point as the
    ``github_org_id`` match, reached through the auto-create branch — so guarding
    only the first branch would leave the gate bypassable by choosing an account
    whose login collides with the victim's tenant id.

    This case is NOT in the approved design; it was found while implementing it and
    is fixed with the same standing check.
    """

    async def test_login_slug_colliding_with_a_foreign_tenant_is_refused(self, db_session: AsyncSession, attacker, _no_aws):
        # A victim tenant with NO github_org_id, so the first branch cannot match —
        # only the slug collision can reach it.
        db_session.add(
            Organization(
                id="target-workspace",
                name="Target Workspace",
                aws_accounts=[],
                role_mappings={},
                settings={},
            )
        )
        await db_session.commit()
        await _write_nonce(db_session)

        with patch("src.admin.connections.tenant_secret.seed_tenant_github_app_secret", new_callable=AsyncMock) as mock_seed:
            with pytest.raises(PermissionError):
                await install_callback(
                    installation_id=INSTALL_ID,
                    setup_action="install",
                    state="jti-4072",
                    db=db_session,
                    # "Target-Workspace" slugifies to the existing "target-workspace".
                    github_client=_github_client(account_login="Target-Workspace", account_github_id=55550001),
                )

        rows = list((await db_session.execute(select(TenantMembership).where(TenantMembership.tenant_id == "target-workspace"))).scalars().all())
        assert rows == []
        mock_seed.assert_not_awaited()
        _no_aws.assert_not_awaited()

    async def test_brand_new_org_shell_still_onboards_its_first_installer(self, db_session: AsyncSession, attacker, _no_aws):
        """#2952 preserved: a shell this install CREATES has no victim to protect.

        The anti-regression half of the slug guard — it must reject collisions with
        *pre-existing* tenants only, never the first-installer onboarding flow that
        creates the tenant it lands in.
        """
        await _write_nonce(db_session)

        with patch("src.admin.connections.tenant_secret.seed_tenant_github_app_secret", new_callable=AsyncMock):
            result = await install_callback(
                installation_id=INSTALL_ID,
                setup_action="install",
                state="jti-4072",
                db=db_session,
                github_client=_github_client(account_login="Brand-New-Org", account_github_id=66660001),
            )

        assert result["success"] is True

        created = (await db_session.execute(select(Organization).where(Organization.id == "brand-new-org"))).scalar_one()
        assert created.github_org_id == "66660001"

        membership = (
            await db_session.execute(
                select(TenantMembership).where(
                    TenantMembership.user_id == "attacker-user-001",
                    TenantMembership.tenant_id == "brand-new-org",
                )
            )
        ).scalar_one()
        assert membership.role == "org_admin"
