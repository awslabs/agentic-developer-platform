"""Issue #4072 (#11, HIGH): the identity PATCH route is platform-admin only, and
cannot be used to steal another tenant's channel routing.

``PATCH /api/admin/identity/organizations/{org_id}`` was the more dangerous of the
two #11 writers, for two independent reasons:

1. **No in-route authorization at all.** The only gate was the router-level
   ``require_admin`` dependency on the mount. Every other identity-minting route
   in the repo re-checks in the handler; this one relied entirely on the mount,
   which is one refactor away from silently disappearing. Decision D4 requires the
   check to mirror ``admin/tenants/routes.py::link_org_to_tenant``
   (``AccessControl.require_platform_admin``), NOT ``Permission.ORG_UPDATE`` —
   in-repo precedent (#3981/#4018) holds ORG_UPDATE insufficient for
   identity-minting writes because a tenant's own org_admin satisfies it.

2. **It is destructive before it is validated.** The handler deletes EVERY
   ``channel_tenant_map`` row for the target org — GitHub *and* Slack — and
   re-inserts only the entries present in the request body. So a request naming a
   victim's installation did double damage: it stole the victim's GitHub routing,
   and if it were rejected only after the delete had run it would also silently
   drop the victim's Slack routing. That is why the ownership guard has to sit
   before the delete, and why the tests below assert on Slack rows they never
   mention in the request.

Gate discipline (sub-EPIC #4068): the tests assert the OUTCOME — what rows exist
afterwards, what the client was told — not that a guard function was invoked. The
Slack-survival assertions in particular cannot be satisfied by a guard that runs
in the wrong place, which a "was it called" assertion would happily accept.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.identity.organizations_service import OrganizationsService
from src.admin.identity.router import router
from src.admin.identity.schemas import ChannelEntry, ChannelsConfig, OrganizationUpdateRequest
from src.admin.installations.guards import InstallationClaimError
from src.auth.dependencies import get_current_user, require_admin
from src.shared.database import get_db
from src.shared.models.organization import Organization
from src.shared.models.vault import ChannelTenantMap
from src.shared.schemas.auth import TokenContext

pytestmark = pytest.mark.asyncio

VICTIM_INSTALL = "5559991"
VICTIM_SLACK = "T-VICTIM-WORKSPACE"


@pytest.fixture
def mock_identity_index():
    mock = AsyncMock()
    mock.sync_org_channels = AsyncMock()
    mock.delete_org_identities = AsyncMock()
    return mock


@pytest.fixture
def mock_cognito_sync():
    mock = AsyncMock()
    mock.ensure_org_group = AsyncMock(return_value=True)
    return mock


async def _mk_org(db: AsyncSession, org_id: str, *, installation_ids: list[str] | None = None) -> Organization:
    org = Organization(
        id=org_id,
        name=f"Org {org_id}",
        aws_accounts=[],
        role_mappings={},
        settings={},
        github_installation_ids=installation_ids or [],
    )
    db.add(org)
    await db.commit()
    return org


# ---------------------------------------------------------------------------
# Half 1 — authorization (D4)
# ---------------------------------------------------------------------------


class TestPatchEnforcesPlatformAdminInTheRoute:
    """The authority half (D4) — scoped honestly.

    Be precise about what this does and does not close. The router-level
    ``require_admin`` dependency checks ``TokenContext.is_admin``, which is
    PLATFORM admin only (``auth/dependencies.py`` deliberately excludes org_admin
    per #3981). So an org_admin was *already* refused by the mount: unlike #5,
    this half is defense-in-depth, not a live escalation. It is in scope because
    D4 requires it and because this route's blast radius — rewriting a tenant's
    GitHub and Slack routing — is too large to rest on a gate that lives only in
    the mount, one refactor away from silently disappearing. Every other
    identity-minting route in the repo re-checks in the handler.

    So the test must exercise exactly that: with the mount's gate loosened, does
    the HANDLER still refuse? A test that called ``require_platform_admin``
    directly would pass on pre-fix code (the method already existed) and prove
    nothing about the route — the #4046 failure mode. These go through the real
    ASGI app instead.
    """

    def _client(self, *, user: TokenContext, db: AsyncSession) -> TestClient:
        """An app whose MOUNT gate is a pass-through, to isolate the route's own check."""
        application = FastAPI()
        application.include_router(router)

        async def _override_current_user():
            return user

        async def _override_require_admin():
            # Simulates the mount gate being loosened or refactored away. The
            # route must stand on its own.
            return user

        async def _override_db():
            yield db

        application.dependency_overrides[get_current_user] = _override_current_user
        application.dependency_overrides[require_admin] = _override_require_admin
        application.dependency_overrides[get_db] = _override_db
        return TestClient(application, raise_server_exceptions=False)

    async def test_non_platform_admin_is_refused_by_the_route_itself(
        self, db_session: AsyncSession, org_admin_context: TokenContext, org_admin_membership
    ):
        """403 from the handler even when the mount lets the caller through.

        Takes ``org_admin_membership`` so the caller is a genuine org admin rather
        than resolving to MEMBER via the least-privilege fallback. That fixture
        also seeds the ``org-001`` organization the request targets.
        """
        client = self._client(user=org_admin_context, db=db_session)
        response = client.patch("/api/admin/identity/organizations/org-001", json={"name": "Renamed By Non Platform Admin"})

        assert response.status_code == 403

        # The outcome, not just the status: the write did not happen.
        refreshed = (await db_session.execute(select(Organization).where(Organization.id == "org-001"))).scalar_one()
        assert refreshed.name != "Renamed By Non Platform Admin"

    async def test_platform_admin_is_still_accepted(self, db_session: AsyncSession, platform_admin_context: TokenContext):
        """The legitimate operator path stays open — the guard is not a wall."""
        await _mk_org(db_session, "org-001")

        client = self._client(user=platform_admin_context, db=db_session)
        response = client.patch("/api/admin/identity/organizations/org-001", json={"name": "Renamed By Operator"})

        assert response.status_code == 200
        assert response.json()["name"] == "Renamed By Operator"

    async def test_unknown_org_still_404s_for_a_platform_admin(self, db_session: AsyncSession, platform_admin_context: TokenContext):
        """The authz check must not shadow the not-found case into a 403."""
        client = self._client(user=platform_admin_context, db=db_session)
        response = client.patch("/api/admin/identity/organizations/no-such-org", json={"name": "X"})

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Half 2 — installation ownership, and the destructive-delete ordering
# ---------------------------------------------------------------------------


class TestPatchInstallationOwnership:
    async def test_claiming_another_orgs_installation_is_refused(self, db_session: AsyncSession, mock_identity_index, mock_cognito_sync):
        """Even a platform admin's PATCH may not re-point an owned installation.

        Authority over the *org* is not authority over the *installation*: a
        re-point silently redirects the victim's webhook traffic, so it must be an
        explicit operator remediation (fix Postgres, re-run the backfill) rather
        than a side effect of an ordinary org update.
        """
        await _mk_org(db_session, "victim-org", installation_ids=[VICTIM_INSTALL])
        db_session.add(
            ChannelTenantMap(
                provider="github",
                provider_scope_id=VICTIM_INSTALL,
                installation_id=VICTIM_INSTALL,
                org_id="victim-org",
            )
        )
        await _mk_org(db_session, "attacker-org")
        await db_session.commit()

        svc = OrganizationsService(db_session, identity_index=mock_identity_index, cognito_sync=mock_cognito_sync)

        with pytest.raises(InstallationClaimError) as exc:
            await svc.update_organization(
                "attacker-org",
                OrganizationUpdateRequest(
                    channels=ChannelsConfig(github=[ChannelEntry(installation_id=VICTIM_INSTALL, org_login="victim")]),
                ),
            )

        assert exc.value.status_code == 409

        # The victim keeps its routing row.
        mapping = (await db_session.execute(select(ChannelTenantMap).where(ChannelTenantMap.installation_id == VICTIM_INSTALL))).scalar_one()
        assert mapping.org_id == "victim-org"

        # And the attacker gained nothing.
        attacker = (await db_session.execute(select(Organization).where(Organization.id == "attacker-org"))).scalar_one()
        assert VICTIM_INSTALL not in (attacker.github_installation_ids or [])

    async def test_refused_claim_does_not_wipe_the_targets_slack_routing(self, db_session: AsyncSession, mock_identity_index, mock_cognito_sync):
        """The ordering property (I2), stated as an outcome.

        The handler's ``DELETE FROM channel_tenant_map WHERE org_id = ...`` is not
        scoped by provider. If the ownership guard ran after it, this refused
        request — which mentions no Slack entry at all — would leave the target org
        with no Slack routing, taking out an unrelated channel as collateral. The
        surviving Slack row is the assertion that the guard runs first.
        """
        await _mk_org(db_session, "target-org")
        db_session.add(
            ChannelTenantMap(
                provider="slack",
                provider_scope_id=VICTIM_SLACK,
                org_id="target-org",
            )
        )
        await _mk_org(db_session, "other-org", installation_ids=[VICTIM_INSTALL])
        db_session.add(
            ChannelTenantMap(
                provider="github",
                provider_scope_id=VICTIM_INSTALL,
                installation_id=VICTIM_INSTALL,
                org_id="other-org",
            )
        )
        await db_session.commit()

        svc = OrganizationsService(db_session, identity_index=mock_identity_index, cognito_sync=mock_cognito_sync)

        with pytest.raises(InstallationClaimError):
            await svc.update_organization(
                "target-org",
                OrganizationUpdateRequest(
                    channels=ChannelsConfig(github=[ChannelEntry(installation_id=VICTIM_INSTALL, org_login="other")]),
                ),
            )

        slack_rows = list(
            (
                await db_session.execute(
                    select(ChannelTenantMap).where(
                        ChannelTenantMap.org_id == "target-org",
                        ChannelTenantMap.provider == "slack",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(slack_rows) == 1
        assert slack_rows[0].provider_scope_id == VICTIM_SLACK

    async def test_refused_claim_writes_nothing_to_the_identity_index(self, db_session: AsyncSession, mock_identity_index, mock_cognito_sync):
        """No DDB write-through for a claim that was refused."""
        await _mk_org(db_session, "other-org", installation_ids=[VICTIM_INSTALL])
        db_session.add(
            ChannelTenantMap(
                provider="github",
                provider_scope_id=VICTIM_INSTALL,
                installation_id=VICTIM_INSTALL,
                org_id="other-org",
            )
        )
        await _mk_org(db_session, "attacker-org")
        await db_session.commit()

        svc = OrganizationsService(db_session, identity_index=mock_identity_index, cognito_sync=mock_cognito_sync)

        with pytest.raises(InstallationClaimError):
            await svc.update_organization(
                "attacker-org",
                OrganizationUpdateRequest(
                    channels=ChannelsConfig(github=[ChannelEntry(installation_id=VICTIM_INSTALL, org_login="other")]),
                ),
            )

        mock_identity_index.sync_org_channels.assert_not_awaited()


class TestPatchLegitimateUpdatesStillWork:
    async def test_binding_an_unowned_installation_succeeds(self, db_session: AsyncSession, mock_identity_index, mock_cognito_sync):
        """The ordinary operator flow: nobody owns it, so this PATCH is the claim."""
        await _mk_org(db_session, "own-org")

        svc = OrganizationsService(db_session, identity_index=mock_identity_index, cognito_sync=mock_cognito_sync)
        result = await svc.update_organization(
            "own-org",
            OrganizationUpdateRequest(
                channels=ChannelsConfig(github=[ChannelEntry(installation_id="7770001", org_login="own")]),
            ),
        )

        assert result.github_installation_ids == ["7770001"]
        rows = list((await db_session.execute(select(ChannelTenantMap).where(ChannelTenantMap.org_id == "own-org"))).scalars().all())
        assert [r.installation_id for r in rows] == ["7770001"]

    async def test_reconfirming_our_own_installation_succeeds(self, db_session: AsyncSession, mock_identity_index, mock_cognito_sync):
        """Re-submitting an id this org already holds is idempotent, not a conflict."""
        await _mk_org(db_session, "own-org", installation_ids=["7770002"])
        db_session.add(
            ChannelTenantMap(
                provider="github",
                provider_scope_id="7770002",
                installation_id="7770002",
                org_id="own-org",
            )
        )
        await db_session.commit()

        svc = OrganizationsService(db_session, identity_index=mock_identity_index, cognito_sync=mock_cognito_sync)
        result = await svc.update_organization(
            "own-org",
            OrganizationUpdateRequest(
                channels=ChannelsConfig(github=[ChannelEntry(installation_id="7770002", org_login="own")]),
            ),
        )

        assert result.github_installation_ids == ["7770002"]

    async def test_non_channel_update_is_unaffected(self, db_session: AsyncSession, mock_identity_index, mock_cognito_sync):
        """A rename must neither run the guard nor disturb existing routing rows."""
        await _mk_org(db_session, "own-org", installation_ids=["legacy-unverifiable"])
        db_session.add(ChannelTenantMap(provider="slack", provider_scope_id="T-KEEP", org_id="own-org"))
        await db_session.commit()

        svc = OrganizationsService(db_session, identity_index=mock_identity_index, cognito_sync=mock_cognito_sync)
        result = await svc.update_organization("own-org", OrganizationUpdateRequest(name="Renamed"))

        assert result.name == "Renamed"
        rows = list((await db_session.execute(select(ChannelTenantMap).where(ChannelTenantMap.org_id == "own-org"))).scalars().all())
        assert [r.provider_scope_id for r in rows] == ["T-KEEP"]
