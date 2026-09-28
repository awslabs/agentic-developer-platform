"""Issue #4072 (#11, HIGH): an org may not bind another tenant's installation.

``PUT /admin/organizations/{org_id}`` assigned ``github_installation_ids``
verbatim from the request body. Its authorization (``Permission.ORG_UPDATE`` +
``target_org_id``) confines a caller to their OWN org — which is exactly why the
hole was easy to miss: the caller genuinely is authorized to write to the org
they name. What nothing checked is whether the *installation* they name is
theirs. An org_admin could list a victim's installation id and thereby inherit
the victim's webhook events, agent runs and credential context, without ever
touching a row outside their own org.

Gate discipline (sub-EPIC #4068): each test asserts the OUTCOME — did the
installation get bound, and did the DynamoDB write-through fire — never the
plumbing. No test asserts that a particular guard function was called, because
such a test passes with the guard replaced by a no-op stub, which is the #4046
failure mode this sub-EPIC exists to reject.

Pre-fix these tests fail because the write succeeds: no exception is raised,
``github_installation_ids`` contains the victim's id, and the index sync is
awaited with it.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from src.admin.identity_index import IdentityIndexClient
from src.admin.installations.guards import InstallationClaimError
from src.admin.schemas import OrganizationUpdateRequest
from src.admin.service import AdminService
from src.shared.models.organization import Organization
from src.shared.models.vault import ChannelTenantMap

pytestmark = pytest.mark.asyncio

VICTIM_INSTALL = 5551111
ATTACKER_INSTALL = 5552222


@pytest.fixture
def mock_identity_index():
    client = MagicMock(spec=IdentityIndexClient)
    client.sync_identities_for_org = AsyncMock()
    client.delete_all_for_org = AsyncMock()
    return client


async def _mk_org(db, org_id: str, *, installation_ids: list[str] | None = None) -> Organization:
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


async def _mk_owned_installation(db, org_id: str, installation_id: int) -> None:
    """Give ``org_id`` a corroborated (server-written) claim on the installation.

    ``channel_tenant_map`` is the store only the install callback writes, so a row
    here is the strongest ownership evidence the resolver recognises — the case an
    attacker must not be able to override.
    """
    db.add(
        ChannelTenantMap(
            provider="github",
            provider_scope_id=f"acct-{org_id}",
            installation_id=str(installation_id),
            org_id=org_id,
        )
    )
    await db.commit()


class TestForeignInstallationClaimRefused:
    async def test_claiming_another_orgs_installation_is_refused(self, db_session, mock_identity_index):
        """The victim's installation must not become the attacker's."""
        await _mk_org(db_session, "victim-org")
        await _mk_owned_installation(db_session, "victim-org", VICTIM_INSTALL)
        await _mk_org(db_session, "attacker-org")

        service = AdminService(db=db_session, identity_index=mock_identity_index)

        with pytest.raises(InstallationClaimError) as exc:
            await service.update_organization(
                "attacker-org",
                OrganizationUpdateRequest(github_installation_ids=[str(VICTIM_INSTALL)]),
            )

        # 409, not 403: the request is well-formed and the caller is authorized
        # for the org — the resource is taken.
        assert exc.value.status_code == 409

        # The outcome that matters: nothing was bound.
        attacker = (await db_session.execute(select(Organization).where(Organization.id == "attacker-org"))).scalar_one()
        assert str(VICTIM_INSTALL) not in (attacker.github_installation_ids or [])

    async def test_refused_claim_writes_nothing_to_the_identity_index(self, db_session, mock_identity_index):
        """A refused claim must not reach the DDB write-through.

        The identity-index row is what webhook-ingress reads to route events, so a
        guard that rejected only *after* the write-through would still hand the
        attacker the victim's traffic.
        """
        await _mk_org(db_session, "victim-org")
        await _mk_owned_installation(db_session, "victim-org", VICTIM_INSTALL)
        await _mk_org(db_session, "attacker-org")

        service = AdminService(db=db_session, identity_index=mock_identity_index)

        with pytest.raises(InstallationClaimError):
            await service.update_organization(
                "attacker-org",
                OrganizationUpdateRequest(github_installation_ids=[str(VICTIM_INSTALL)]),
            )

        mock_identity_index.sync_identities_for_org.assert_not_awaited()

    async def test_victim_retains_its_installation(self, db_session, mock_identity_index):
        """The victim's own routing row is untouched by the refused attempt."""
        await _mk_org(db_session, "victim-org", installation_ids=[str(VICTIM_INSTALL)])
        await _mk_owned_installation(db_session, "victim-org", VICTIM_INSTALL)
        await _mk_org(db_session, "attacker-org")

        service = AdminService(db=db_session, identity_index=mock_identity_index)

        with pytest.raises(InstallationClaimError):
            await service.update_organization(
                "attacker-org",
                OrganizationUpdateRequest(github_installation_ids=[str(VICTIM_INSTALL)]),
            )

        mapping = (await db_session.execute(select(ChannelTenantMap).where(ChannelTenantMap.installation_id == str(VICTIM_INSTALL)))).scalar_one()
        assert mapping.org_id == "victim-org"

    async def test_smuggling_a_foreign_id_alongside_a_legitimate_one_is_refused(self, db_session, mock_identity_index):
        """Every ADDED id is checked, not just the first or the only one."""
        await _mk_org(db_session, "victim-org")
        await _mk_owned_installation(db_session, "victim-org", VICTIM_INSTALL)
        await _mk_org(db_session, "attacker-org")

        service = AdminService(db=db_session, identity_index=mock_identity_index)

        with pytest.raises(InstallationClaimError):
            await service.update_organization(
                "attacker-org",
                OrganizationUpdateRequest(github_installation_ids=[str(ATTACKER_INSTALL), str(VICTIM_INSTALL)]),
            )

        attacker = (await db_session.execute(select(Organization).where(Organization.id == "attacker-org"))).scalar_one()
        # The whole update is refused — a partial apply would leave the org in a
        # state neither the caller nor the operator asked for.
        assert str(ATTACKER_INSTALL) not in (attacker.github_installation_ids or [])


class TestLegitimateClaimsStillWork:
    """The guard must not break the flows that legitimately bind installations."""

    async def test_first_claim_of_an_unowned_installation_succeeds(self, db_session, mock_identity_index):
        """Nobody owns it, so this write IS the claim — the common admin case."""
        await _mk_org(db_session, "own-org")

        service = AdminService(db=db_session, identity_index=mock_identity_index)
        result = await service.update_organization(
            "own-org",
            OrganizationUpdateRequest(github_installation_ids=[str(ATTACKER_INSTALL)]),
        )

        assert result.github_installation_ids == [str(ATTACKER_INSTALL)]

    async def test_reconfirming_an_installation_we_already_own_succeeds(self, db_session, mock_identity_index):
        """Idempotent re-submission of our own id is not a cross-tenant claim."""
        await _mk_org(db_session, "own-org", installation_ids=[str(ATTACKER_INSTALL)])
        await _mk_owned_installation(db_session, "own-org", ATTACKER_INSTALL)

        service = AdminService(db=db_session, identity_index=mock_identity_index)
        result = await service.update_organization(
            "own-org",
            OrganizationUpdateRequest(github_installation_ids=[str(ATTACKER_INSTALL)]),
        )

        assert result.github_installation_ids == [str(ATTACKER_INSTALL)]

    async def test_removing_an_id_is_never_blocked(self, db_session, mock_identity_index):
        """Only ADDED ids are checked, so shrinking the list always works.

        This is the anti-outage property: a pre-existing entry that the resolver
        cannot vouch for (self-asserted, no corroborating row) must not make every
        later update to that org fail permanently — including the update that
        removes it.
        """
        await _mk_org(db_session, "own-org", installation_ids=["legacy-unverifiable", str(ATTACKER_INSTALL)])

        service = AdminService(db=db_session, identity_index=mock_identity_index)
        result = await service.update_organization(
            "own-org",
            OrganizationUpdateRequest(github_installation_ids=[str(ATTACKER_INSTALL)]),
        )

        assert result.github_installation_ids == [str(ATTACKER_INSTALL)]

    async def test_unrelated_field_update_does_not_touch_installations(self, db_session, mock_identity_index):
        """Renaming an org must not run — or trip — the ownership guard."""
        await _mk_org(db_session, "own-org", installation_ids=["legacy-unverifiable"])

        service = AdminService(db=db_session, identity_index=mock_identity_index)
        result = await service.update_organization("own-org", OrganizationUpdateRequest(name="Renamed"))

        assert result.name == "Renamed"
        assert result.github_installation_ids == ["legacy-unverifiable"]
