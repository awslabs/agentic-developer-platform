"""Issue #4842 (EPIC #4839 · C3): the org GitHub-connection attach/detach lifecycle.

An admin-created org starts with no GitHub at all, so "connect one later" and
"disconnect it" have to be operations. These tests pin the two behaviours the
issue's impact analysis names as the ways this could ship broken:

1. **Attach must respect one-installation-one-org.** Binding an installation that
   another tenant already owns is the cross-tenant confusion class migration 027
   exists to prevent. Attach therefore goes through the same ownership assertion
   the #4072 writers use, and must refuse **fail-closed** — before any write.
2. **Detach must not orphan identity-index rows.** A leftover index row is stale
   webhook routing: the UI shows the org as disconnected while events keep
   resolving to it.

Assertion discipline (sub-EPIC #4068): these assert the OUTCOME — which rows
exist afterwards, what the client was told, which index calls were made — not that
a guard function was invoked. A "was the guard called" assertion passes against a
guard placed *after* the destructive write, which is precisely the bug worth
catching; the victim-rows-survive assertions cannot be satisfied that way.

SQLite caveat: this suite runs on SQLite (``tests/conftest.py``), which builds
neither migration 027's partial unique index nor any Postgres-only constraint. So
these prove the *application* enforces the invariant — which is the point, since
the guard exists so the caller gets a 409 with a message rather than a 500 from a
constraint violation. They cannot themselves prove the index is present.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.installations.guards import InstallationClaimError
from src.admin.org_connections.routes import router
from src.admin.org_connections.schemas import GitHubConnectionAttachRequest
from src.admin.org_connections.service import (
    ConnectionNotFoundError,
    OrganizationNotFoundError,
    OrgConnectionsService,
)
from src.auth.dependencies import get_current_user, require_admin
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.organization import Organization
from src.shared.models.vault import ChannelTenantMap
from src.shared.schemas.auth import TokenContext

pytestmark = pytest.mark.asyncio

OWNER_ORG = "acme"
VICTIM_ORG = "victim-co"
VICTIM_INSTALL = 5559991
VICTIM_ACCOUNT = "77771"
VICTIM_SLACK = "T-VICTIM-WORKSPACE"
FREE_INSTALL = 124731131
FREE_ACCOUNT = "88881"


@pytest.fixture
def index() -> AsyncMock:
    mock = AsyncMock()
    mock.sync_org_channels = AsyncMock()
    return mock


async def _mk_org(db: AsyncSession, org_id: str, *, installation_ids: list[str] | None = None, github_org_id: str | None = None) -> Organization:
    org = Organization(
        id=org_id,
        name=f"Org {org_id}",
        aws_accounts=[],
        role_mappings={},
        settings={},
        github_installation_ids=installation_ids or [],
        github_org_id=github_org_id,
    )
    db.add(org)
    await db.commit()
    return org


async def _mk_binding(db: AsyncSession, org_id: str, *, installation_id: int, account_id: str) -> None:
    """A CORROBORATED claim — the server-written row that grants ownership."""
    db.add(
        ChannelTenantMap(
            provider="github",
            provider_scope_id=account_id,
            installation_id=str(installation_id),
            org_id=org_id,
        )
    )
    await db.commit()


async def _github_rows(db: AsyncSession, org_id: str) -> list[ChannelTenantMap]:
    return list(
        (
            await db.execute(
                select(ChannelTenantMap).where(
                    ChannelTenantMap.provider == "github",
                    ChannelTenantMap.org_id == org_id,
                )
            )
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# Attach — the one-installation-one-org invariant
# ---------------------------------------------------------------------------


class TestAttachFailsClosed:
    async def test_attach_refuses_an_installation_owned_by_another_org(self, db_session: AsyncSession, index: AsyncMock):
        """409, and the victim keeps its binding.

        The cross-tenant class from the issue's impact analysis: attaching a
        victim's installation would hand this org the victim's webhook events,
        agent runs and credential context.
        """
        await _mk_org(db_session, VICTIM_ORG, installation_ids=[str(VICTIM_INSTALL)], github_org_id=VICTIM_ACCOUNT)
        await _mk_binding(db_session, VICTIM_ORG, installation_id=VICTIM_INSTALL, account_id=VICTIM_ACCOUNT)
        await _mk_org(db_session, OWNER_ORG)

        svc = OrgConnectionsService(db_session, identity_index=index)
        with pytest.raises(InstallationClaimError) as exc:
            await svc.attach_github(OWNER_ORG, GitHubConnectionAttachRequest(installation_id=VICTIM_INSTALL, github_org_id=VICTIM_ACCOUNT))

        assert exc.value.status_code == 409

        # The victim's binding is intact and still points at the victim.
        victim_rows = await _github_rows(db_session, VICTIM_ORG)
        assert [r.installation_id for r in victim_rows] == [str(VICTIM_INSTALL)]
        victim = await db_session.get(Organization, VICTIM_ORG)
        assert victim.github_installation_ids == [str(VICTIM_INSTALL)]

    async def test_a_refused_attach_writes_nothing(self, db_session: AsyncSession, index: AsyncMock):
        """Fail-closed means the guard runs BEFORE any mutation.

        The attacking org must gain no row and no assertion — not even a partial
        one it could later parlay into a claim. Also asserts the identity index was
        never touched: a refused attach that still wrote to DynamoDB would make the
        binding routable in the webhook Lambda despite the 409.
        """
        await _mk_org(db_session, VICTIM_ORG, installation_ids=[str(VICTIM_INSTALL)], github_org_id=VICTIM_ACCOUNT)
        await _mk_binding(db_session, VICTIM_ORG, installation_id=VICTIM_INSTALL, account_id=VICTIM_ACCOUNT)
        await _mk_org(db_session, OWNER_ORG)

        svc = OrgConnectionsService(db_session, identity_index=index)
        with pytest.raises(InstallationClaimError):
            await svc.attach_github(OWNER_ORG, GitHubConnectionAttachRequest(installation_id=VICTIM_INSTALL, github_org_id=VICTIM_ACCOUNT))

        attacker = await db_session.get(Organization, OWNER_ORG)
        assert attacker.github_installation_ids == []
        assert attacker.github_org_id is None
        assert await _github_rows(db_session, OWNER_ORG) == []
        index.sync_org_channels.assert_not_awaited()

    async def test_attach_refuses_a_scope_collision_with_another_orgs_row(self, db_session: AsyncSession, index: AsyncMock):
        """An UNCLAIMED installation + another org's account scope → 409, victim intact.

        #4915 review H1, the update-in-place variant of the delete-then-insert
        bypass: the claimability guard keys on the INSTALLATION (unclaimed →
        proceed), but the write used to upsert by ACCOUNT SCOPE — so a stale or
        mistyped github_org_id matching the victim's scope row would silently
        re-home that row (new installation_id, new org_id) without ever
        tripping migration 027's unique index, because an UPDATE keeps
        installation_id unique. The victim's only ownership-granting row
        becomes the attacker's; their webhook routing dies UNATTESTABLE.
        """
        await _mk_org(db_session, VICTIM_ORG, installation_ids=[str(VICTIM_INSTALL)], github_org_id=VICTIM_ACCOUNT)
        await _mk_binding(db_session, VICTIM_ORG, installation_id=VICTIM_INSTALL, account_id=VICTIM_ACCOUNT)
        await _mk_org(db_session, OWNER_ORG)

        svc = OrgConnectionsService(db_session, identity_index=index)
        with pytest.raises(InstallationClaimError) as exc:
            # FREE_INSTALL is genuinely unclaimed — the installation guard passes.
            # The collision is on the victim's ACCOUNT id.
            await svc.attach_github(
                OWNER_ORG,
                GitHubConnectionAttachRequest(installation_id=FREE_INSTALL, github_org_id=VICTIM_ACCOUNT),
            )

        assert exc.value.status_code == 409

        # The victim's row is untouched: same installation, same owner.
        victim_rows = await _github_rows(db_session, VICTIM_ORG)
        assert [(r.installation_id, r.org_id) for r in victim_rows] == [(str(VICTIM_INSTALL), VICTIM_ORG)]
        # The attacker gained no map row and the index was never told anything.
        assert await _github_rows(db_session, OWNER_ORG) == []
        index.sync_org_channels.assert_not_awaited()

    async def test_attach_refuses_a_disputed_installation(self, db_session: AsyncSession, index: AsyncMock):
        """An installation two tenants claim is AMBIGUOUS → 409, never a guess.

        Seeded as two corroborated rows, the shape migration 026 quarantines.
        Resolving this by picking a winner is the wrong-tenant-routing bug.
        """
        await _mk_org(db_session, VICTIM_ORG)
        await _mk_org(db_session, "third-party")
        await _mk_org(db_session, OWNER_ORG)
        await _mk_binding(db_session, VICTIM_ORG, installation_id=VICTIM_INSTALL, account_id=VICTIM_ACCOUNT)
        await _mk_binding(db_session, "third-party", installation_id=VICTIM_INSTALL, account_id="99992")

        svc = OrgConnectionsService(db_session, identity_index=index)
        with pytest.raises(InstallationClaimError) as exc:
            await svc.attach_github(OWNER_ORG, GitHubConnectionAttachRequest(installation_id=VICTIM_INSTALL))

        assert exc.value.status_code == 409
        assert await _github_rows(db_session, OWNER_ORG) == []

    async def test_attach_refuses_a_self_asserted_unattestable_claim(self, db_session: AsyncSession, index: AsyncMock):
        """403 when a claim exists but nothing can substantiate it.

        A victim org listing the installation in ``github_installation_ids`` with
        no ``channel_tenant_map`` row behind it is UNATTESTABLE — the resolver
        refuses to let that assertion GRANT ownership, and equally refuses to let
        another org bind past it. Withholding trust in both directions is the
        fail-closed half of ·A0's contract.
        """
        await _mk_org(db_session, VICTIM_ORG, installation_ids=[str(VICTIM_INSTALL)])
        await _mk_org(db_session, OWNER_ORG)

        svc = OrgConnectionsService(db_session, identity_index=index)
        with pytest.raises(InstallationClaimError) as exc:
            await svc.attach_github(OWNER_ORG, GitHubConnectionAttachRequest(installation_id=VICTIM_INSTALL))

        assert exc.value.status_code == 403
        assert await _github_rows(db_session, OWNER_ORG) == []


class TestAttachHappyPath:
    async def test_attach_writes_both_stores(self, db_session: AsyncSession, index: AsyncMock):
        """An unclaimed installation binds, and BOTH records are written.

        Both, because they carry different authority: the ``channel_tenant_map``
        row is the only claim that can grant ownership (so routing depends on it),
        while ``github_installation_ids`` is what the org's own read paths use. One
        without the other is a half-connection.
        """
        await _mk_org(db_session, OWNER_ORG)
        svc = OrgConnectionsService(db_session, identity_index=index)

        result = await svc.attach_github(
            OWNER_ORG,
            GitHubConnectionAttachRequest(installation_id=FREE_INSTALL, github_org_id=FREE_ACCOUNT, github_org_login="acme-eng"),
        )

        assert result.installation_id == str(FREE_INSTALL)
        assert result.routable is True

        org = await db_session.get(Organization, OWNER_ORG)
        assert org.github_installation_ids == [str(FREE_INSTALL)]
        assert org.github_org_id == FREE_ACCOUNT

        rows = await _github_rows(db_session, OWNER_ORG)
        assert [r.installation_id for r in rows] == [str(FREE_INSTALL)]
        # Keyed on the ACCOUNT scope, matching the other two writers so all three
        # agree on one row per GitHub account (#4070 D1).
        assert rows[0].provider_scope_id == FREE_ACCOUNT

    async def test_attach_makes_the_connection_routable_in_the_index(self, db_session: AsyncSession, index: AsyncMock):
        """The webhook Lambda routes off DynamoDB, so the index write is the point.

        Without it the binding is live in Postgres and invisible to dispatch —
        the post-merge smoke test in the issue ("verify webhook resolution routes
        to it") would fail while every SQL assertion passed.
        """
        await _mk_org(db_session, OWNER_ORG)
        svc = OrgConnectionsService(db_session, identity_index=index)

        await svc.attach_github(OWNER_ORG, GitHubConnectionAttachRequest(installation_id=FREE_INSTALL, github_org_id=FREE_ACCOUNT))

        index.sync_org_channels.assert_awaited_once()
        kwargs = index.sync_org_channels.await_args.kwargs
        assert kwargs["org_id"] == OWNER_ORG
        assert kwargs["github_installation_ids"] == [str(FREE_INSTALL)]

    async def test_attach_is_idempotent(self, db_session: AsyncSession, index: AsyncMock):
        """Re-attaching what the org already owns succeeds without duplicating.

        The guard treats owned-by-us as an idempotent re-confirmation, so an
        operator retrying after a timeout must not get a 409 or a second row.
        """
        await _mk_org(db_session, OWNER_ORG)
        svc = OrgConnectionsService(db_session, identity_index=index)
        req = GitHubConnectionAttachRequest(installation_id=FREE_INSTALL, github_org_id=FREE_ACCOUNT)

        await svc.attach_github(OWNER_ORG, req)
        await svc.attach_github(OWNER_ORG, req)

        org = await db_session.get(Organization, OWNER_ORG)
        assert org.github_installation_ids == [str(FREE_INSTALL)]
        assert len(await _github_rows(db_session, OWNER_ORG)) == 1

    async def test_reattach_with_a_different_scope_hint_does_not_duplicate(self, db_session: AsyncSession, index: AsyncMock):
        """A retry that omits github_org_id must find the row it made last time.

        #4915 review M1: the first attach keys the map row on the account scope;
        a retry without github_org_id computes a different scope
        (the installation id itself), and a scope-only lookup would INSERT a
        second row carrying the same installation_id — an unmapped
        IntegrityError (500) on Postgres, a silent duplicate on SQLite. The
        canonical-key (installation_id) lookup makes the retry idempotent.
        """
        await _mk_org(db_session, OWNER_ORG)
        svc = OrgConnectionsService(db_session, identity_index=index)

        await svc.attach_github(OWNER_ORG, GitHubConnectionAttachRequest(installation_id=FREE_INSTALL, github_org_id=FREE_ACCOUNT))
        # Same installation, no scope hint at all this time.
        await svc.attach_github(OWNER_ORG, GitHubConnectionAttachRequest(installation_id=FREE_INSTALL))

        rows = await _github_rows(db_session, OWNER_ORG)
        assert len(rows) == 1
        assert rows[0].installation_id == str(FREE_INSTALL)
        # The original account-scoped key survives; the retry did not re-key it.
        assert rows[0].provider_scope_id == FREE_ACCOUNT

    async def test_attach_to_a_missing_org_is_refused(self, db_session: AsyncSession, index: AsyncMock):
        """No org → no binding. A map row whose FK target is absent is an orphan."""
        svc = OrgConnectionsService(db_session, identity_index=index)
        with pytest.raises(OrganizationNotFoundError):
            await svc.attach_github("no-such-org", GitHubConnectionAttachRequest(installation_id=FREE_INSTALL))

    async def test_index_failure_does_not_undo_a_committed_attach(self, db_session: AsyncSession):
        """DynamoDB being down must not report a committed binding as a failure.

        The index write is post-commit and best-effort by design. Propagating its
        error would tell the operator the attach failed while Postgres holds it —
        they would retry, and the retry's outcome depends on a guard that now sees
        their own claim.
        """
        await _mk_org(db_session, OWNER_ORG)
        failing = AsyncMock()
        failing.sync_org_channels = AsyncMock(side_effect=RuntimeError("DDB unavailable"))
        svc = OrgConnectionsService(db_session, identity_index=failing)

        result = await svc.attach_github(OWNER_ORG, GitHubConnectionAttachRequest(installation_id=FREE_INSTALL, github_org_id=FREE_ACCOUNT))

        assert result.installation_id == str(FREE_INSTALL)
        org = await db_session.get(Organization, OWNER_ORG)
        assert org.github_installation_ids == [str(FREE_INSTALL)]


# ---------------------------------------------------------------------------
# Detach — no orphaned index rows, no collateral damage
# ---------------------------------------------------------------------------


class TestDetach:
    async def test_detach_clears_both_stores(self, db_session: AsyncSession, index: AsyncMock):
        """Both records go, or the leftover half keeps a half-connection alive."""
        await _mk_org(db_session, OWNER_ORG, installation_ids=[str(FREE_INSTALL)], github_org_id=FREE_ACCOUNT)
        await _mk_binding(db_session, OWNER_ORG, installation_id=FREE_INSTALL, account_id=FREE_ACCOUNT)

        svc = OrgConnectionsService(db_session, identity_index=index)
        result = await svc.detach_github(OWNER_ORG, FREE_INSTALL)

        assert result.detached is True
        org = await db_session.get(Organization, OWNER_ORG)
        assert org.github_installation_ids == []
        assert await _github_rows(db_session, OWNER_ORG) == []

    async def test_detach_keeps_identity_fields_while_a_map_only_install_survives(self, db_session: AsyncSession, index: AsyncMock):
        """Step 3 must ask both stores whether anything is left.

        A personal install is never written to ``github_installation_ids``
        (``install_callback`` appends only for ``account_type == "Organization"``),
        so an org can legitimately have an empty column and live
        ``channel_tenant_map`` rows. Reading only the column made "nothing left"
        true while a sibling was still connected, nulling ``github_org_id`` and
        making the survivor UNATTESTABLE — the routing breakage the step's own
        comment says it avoids.
        """
        await _mk_org(db_session, OWNER_ORG, installation_ids=[], github_org_id=FREE_ACCOUNT)
        await _mk_binding(db_session, OWNER_ORG, installation_id=FREE_INSTALL, account_id=FREE_ACCOUNT)
        await _mk_binding(db_session, OWNER_ORG, installation_id=VICTIM_INSTALL, account_id="77772")

        svc = OrgConnectionsService(db_session, identity_index=index)
        await svc.detach_github(OWNER_ORG, FREE_INSTALL)

        org = await db_session.get(Organization, OWNER_ORG)
        assert org.github_org_id == FREE_ACCOUNT
        assert [r.installation_id for r in await _github_rows(db_session, OWNER_ORG)] == [str(VICTIM_INSTALL)]

    async def test_detach_of_the_last_install_still_clears_identity_fields(self, db_session: AsyncSession, index: AsyncMock):
        """The other half of the guard, so widening it cannot disable it."""
        await _mk_org(db_session, OWNER_ORG, installation_ids=[str(FREE_INSTALL)], github_org_id=FREE_ACCOUNT)
        await _mk_binding(db_session, OWNER_ORG, installation_id=FREE_INSTALL, account_id=FREE_ACCOUNT)

        svc = OrgConnectionsService(db_session, identity_index=index)
        await svc.detach_github(OWNER_ORG, FREE_INSTALL)

        org = await db_session.get(Organization, OWNER_ORG)
        assert org.github_org_id is None
        assert org.github_app_id is None

    async def test_detach_removes_the_identity_index_row(self, db_session: AsyncSession, index: AsyncMock):
        """The stale-routing failure mode from the issue's impact analysis.

        Asserts the OLD list is passed alongside the new one: that diff is what
        makes ``sync_org_channels`` issue a DELETE for the id that disappeared. A
        call that omitted it would upsert the survivors and silently leave the
        detached installation routable.
        """
        await _mk_org(db_session, OWNER_ORG, installation_ids=[str(FREE_INSTALL)], github_org_id=FREE_ACCOUNT)
        await _mk_binding(db_session, OWNER_ORG, installation_id=FREE_INSTALL, account_id=FREE_ACCOUNT)

        svc = OrgConnectionsService(db_session, identity_index=index)
        await svc.detach_github(OWNER_ORG, FREE_INSTALL)

        index.sync_org_channels.assert_awaited_once()
        kwargs = index.sync_org_channels.await_args.kwargs
        assert kwargs["github_installation_ids"] == []
        assert kwargs["old_github_installation_ids"] == [str(FREE_INSTALL)]

    async def test_detach_warns_that_webhook_dispatch_stops(self, db_session: AsyncSession, index: AsyncMock):
        """The consequence is in the response body, not only in a log line.

        Fail-closed dispatch is intended behaviour, but an operator detaching to
        "tidy up" needs to see it at the moment they do it.
        """
        await _mk_org(db_session, OWNER_ORG, installation_ids=[str(FREE_INSTALL)], github_org_id=FREE_ACCOUNT)
        await _mk_binding(db_session, OWNER_ORG, installation_id=FREE_INSTALL, account_id=FREE_ACCOUNT)

        svc = OrgConnectionsService(db_session, identity_index=index)
        result = await svc.detach_github(OWNER_ORG, FREE_INSTALL)

        assert "Webhook dispatch" in result.warning
        assert "fail-closed" in result.warning

    async def test_detach_leaves_other_providers_alone(self, db_session: AsyncSession, index: AsyncMock):
        """Detaching GitHub must not drop the org's Slack routing.

        The precedent this guards against is real: the identity PATCH writer's
        unscoped delete-by-org removed every ``channel_tenant_map`` row for a
        tenant regardless of provider (#4072). Slack is asserted here even though
        no part of the request mentions it — exactly because the request doesn't.
        """
        await _mk_org(db_session, OWNER_ORG, installation_ids=[str(FREE_INSTALL)], github_org_id=FREE_ACCOUNT)
        await _mk_binding(db_session, OWNER_ORG, installation_id=FREE_INSTALL, account_id=FREE_ACCOUNT)
        db_session.add(ChannelTenantMap(provider="slack", provider_scope_id=VICTIM_SLACK, org_id=OWNER_ORG))
        await db_session.commit()

        svc = OrgConnectionsService(db_session, identity_index=index)
        await svc.detach_github(OWNER_ORG, FREE_INSTALL)

        slack = (
            (
                await db_session.execute(
                    select(ChannelTenantMap).where(
                        ChannelTenantMap.provider == "slack",
                        ChannelTenantMap.org_id == OWNER_ORG,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [r.provider_scope_id for r in slack] == [VICTIM_SLACK]

    async def test_detach_keeps_github_org_id_while_other_installations_remain(self, db_session: AsyncSession, index: AsyncMock):
        """``github_org_id`` is per-ACCOUNT, so clearing it early breaks survivors.

        Without it the resolver reports UNATTESTABLE for the remaining
        installation and its routing dies — a detach of one connection taking out
        another.
        """
        second = 999222
        await _mk_org(db_session, OWNER_ORG, installation_ids=[str(FREE_INSTALL), str(second)], github_org_id=FREE_ACCOUNT)
        await _mk_binding(db_session, OWNER_ORG, installation_id=FREE_INSTALL, account_id=FREE_ACCOUNT)

        svc = OrgConnectionsService(db_session, identity_index=index)
        await svc.detach_github(OWNER_ORG, FREE_INSTALL)

        org = await db_session.get(Organization, OWNER_ORG)
        assert org.github_installation_ids == [str(second)]
        assert org.github_org_id == FREE_ACCOUNT

    async def test_detach_clears_github_org_id_when_nothing_remains(self, db_session: AsyncSession, index: AsyncMock):
        """Fully disconnected means no residual GitHub identity on the org."""
        await _mk_org(db_session, OWNER_ORG, installation_ids=[str(FREE_INSTALL)], github_org_id=FREE_ACCOUNT)
        await _mk_binding(db_session, OWNER_ORG, installation_id=FREE_INSTALL, account_id=FREE_ACCOUNT)

        svc = OrgConnectionsService(db_session, identity_index=index)
        await svc.detach_github(OWNER_ORG, FREE_INSTALL)

        org = await db_session.get(Organization, OWNER_ORG)
        assert org.github_org_id is None

    async def test_detach_of_an_unconnected_installation_is_not_found(self, db_session: AsyncSession, index: AsyncMock):
        await _mk_org(db_session, OWNER_ORG)
        svc = OrgConnectionsService(db_session, identity_index=index)
        with pytest.raises(ConnectionNotFoundError):
            await svc.detach_github(OWNER_ORG, FREE_INSTALL)

    async def test_detach_cannot_reach_another_orgs_connection(self, db_session: AsyncSession, index: AsyncMock):
        """A platform admin naming org A cannot detach org B's installation.

        Not-found rather than forbidden: the request names a connection that does
        not exist on the org it targets, and this route has no business confirming
        or denying another tenant's bindings.
        """
        await _mk_org(db_session, VICTIM_ORG, installation_ids=[str(VICTIM_INSTALL)], github_org_id=VICTIM_ACCOUNT)
        await _mk_binding(db_session, VICTIM_ORG, installation_id=VICTIM_INSTALL, account_id=VICTIM_ACCOUNT)
        await _mk_org(db_session, OWNER_ORG)

        svc = OrgConnectionsService(db_session, identity_index=index)
        with pytest.raises(ConnectionNotFoundError):
            await svc.detach_github(OWNER_ORG, VICTIM_INSTALL)

        victim_rows = await _github_rows(db_session, VICTIM_ORG)
        assert [r.installation_id for r in victim_rows] == [str(VICTIM_INSTALL)]
        index.sync_org_channels.assert_not_awaited()


# ---------------------------------------------------------------------------
# Listing — the Connections tab's read model
# ---------------------------------------------------------------------------


class TestListConnections:
    async def test_a_half_written_connection_is_visible_and_marked_unroutable(self, db_session: AsyncSession, index: AsyncMock):
        """An id asserted by the org with no map row shows up, flagged.

        Hiding it would be worse than showing it: the operator sees "no
        connections" while the org's own column claims one, with nothing to act
        on. ``routable=False`` is the honest answer — present, but webhooks for it
        will not resolve.
        """
        await _mk_org(db_session, OWNER_ORG, installation_ids=[str(FREE_INSTALL)])

        result = await OrgConnectionsService(db_session, identity_index=index).list_connections(OWNER_ORG)

        assert result.total == 1
        assert result.connections[0].installation_id == str(FREE_INSTALL)
        assert result.connections[0].routable is False

    async def test_a_fully_bound_connection_is_routable(self, db_session: AsyncSession, index: AsyncMock):
        await _mk_org(db_session, OWNER_ORG, installation_ids=[str(FREE_INSTALL)], github_org_id=FREE_ACCOUNT)
        await _mk_binding(db_session, OWNER_ORG, installation_id=FREE_INSTALL, account_id=FREE_ACCOUNT)

        result = await OrgConnectionsService(db_session, identity_index=index).list_connections(OWNER_ORG)

        assert [c.routable for c in result.connections] == [True]

    async def test_listing_a_missing_org_is_refused(self, db_session: AsyncSession, index: AsyncMock):
        with pytest.raises(OrganizationNotFoundError):
            await OrgConnectionsService(db_session, identity_index=index).list_connections("no-such-org")


# ---------------------------------------------------------------------------
# HTTP surface: status codes and the in-handler authorization re-check
# ---------------------------------------------------------------------------


def _client(*, user: TokenContext, db: AsyncSession) -> TestClient:
    """An app whose MOUNT gate is a pass-through, isolating each route's own check.

    Overriding ``require_admin`` simulates the mount gate being loosened or
    refactored away. Calling ``require_platform_admin`` directly instead would
    pass against a route that never checks — the #4046 failure mode.
    """
    application = FastAPI()
    application.include_router(router)

    @application.exception_handler(BedrockGatewayError)
    async def _gateway_error(_request, exc: BedrockGatewayError):
        # Mirrors app.py's registered handler so InstallationClaimError renders
        # with its own status code rather than surfacing as a 500.
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content={"error": exc.error, "message": exc.message})

    async def _user():
        return user

    async def _db():
        yield db

    application.dependency_overrides[get_current_user] = _user
    application.dependency_overrides[require_admin] = _user
    application.dependency_overrides[get_db] = _db
    return TestClient(application, raise_server_exceptions=False)


@pytest.fixture
def no_real_index(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stub the identity-index writer for the HTTP tests.

    The routes construct the service themselves (no injection point), and the
    real writer builds boto3 clients in its constructor. Patched at the lazy
    ``_writer`` accessor — which exists for exactly this reason — so these tests
    exercise the real route/service wiring without reaching AWS.
    """
    stub = AsyncMock()
    stub.sync_org_channels = AsyncMock()
    monkeypatch.setattr(OrgConnectionsService, "_writer", lambda self: stub)
    return stub


@pytest.mark.usefixtures("no_real_index")
class TestRouteContract:
    async def test_non_platform_admin_is_refused_by_the_handler_itself(
        self, db_session: AsyncSession, org_admin_context: TokenContext, org_admin_membership
    ):
        """403 from each handler even when the mount lets the caller through.

        These routes can target ANY org, so an org admin holding authority over
        their own tenant is exactly the caller that must not reach them.
        """
        client = _client(user=org_admin_context, db=db_session)

        assert client.get("/admin/organizations/org-001/connections/github").status_code == 403
        assert client.post("/admin/organizations/org-001/connections/github", json={"installation_id": FREE_INSTALL}).status_code == 403
        assert client.delete(f"/admin/organizations/org-001/connections/github/{FREE_INSTALL}").status_code == 403

    async def test_attach_returns_409_for_a_cross_tenant_claim(self, db_session: AsyncSession, platform_admin_context: TokenContext):
        """The refusal reaches the client as a 409, not a 500.

        This is why the guard raises ``BedrockGatewayError`` and the route does not
        catch it: a fail-closed refusal has to arrive as an actionable conflict.
        """
        await _mk_org(db_session, VICTIM_ORG, installation_ids=[str(VICTIM_INSTALL)], github_org_id=VICTIM_ACCOUNT)
        await _mk_binding(db_session, VICTIM_ORG, installation_id=VICTIM_INSTALL, account_id=VICTIM_ACCOUNT)
        await _mk_org(db_session, OWNER_ORG)

        client = _client(user=platform_admin_context, db=db_session)
        response = client.post(
            f"/admin/organizations/{OWNER_ORG}/connections/github",
            json={"installation_id": VICTIM_INSTALL, "github_org_id": VICTIM_ACCOUNT},
        )

        assert response.status_code == 409

    async def test_attach_returns_404_for_an_unknown_org(self, db_session: AsyncSession, platform_admin_context: TokenContext):
        client = _client(user=platform_admin_context, db=db_session)
        response = client.post("/admin/organizations/no-such-org/connections/github", json={"installation_id": FREE_INSTALL})
        assert response.status_code == 404

    async def test_detach_returns_404_for_an_unconnected_installation(self, db_session: AsyncSession, platform_admin_context: TokenContext):
        await _mk_org(db_session, OWNER_ORG)
        client = _client(user=platform_admin_context, db=db_session)
        response = client.delete(f"/admin/organizations/{OWNER_ORG}/connections/github/{FREE_INSTALL}")
        assert response.status_code == 404

    async def test_installation_id_must_be_positive(self, db_session: AsyncSession, platform_admin_context: TokenContext):
        """A GitHub installation id is always a positive integer."""
        await _mk_org(db_session, OWNER_ORG)
        client = _client(user=platform_admin_context, db=db_session)
        response = client.post(f"/admin/organizations/{OWNER_ORG}/connections/github", json={"installation_id": 0})
        assert response.status_code == 422

    async def test_the_lifecycle_round_trips_over_http(self, db_session: AsyncSession, platform_admin_context: TokenContext):
        """The issue's smoke test, in CI: GitHub-free org → attach → detach.

        Exercises the sequence end-to-end through the real ASGI app so the route
        wiring, schemas and status codes are proven together, not just the service.
        """
        await _mk_org(db_session, OWNER_ORG)
        client = _client(user=platform_admin_context, db=db_session)
        base = f"/admin/organizations/{OWNER_ORG}/connections/github"

        assert client.get(base).json()["total"] == 0

        attached = client.post(base, json={"installation_id": FREE_INSTALL, "github_org_id": FREE_ACCOUNT, "github_org_login": "acme-eng"})
        assert attached.status_code == 201
        assert attached.json()["routable"] is True

        listed = client.get(base).json()
        assert listed["total"] == 1
        assert listed["connections"][0]["routable"] is True

        detached = client.delete(f"{base}/{FREE_INSTALL}")
        assert detached.status_code == 200
        assert detached.json()["detached"] is True

        assert client.get(base).json()["total"] == 0
