"""Attach / detach an organization's GitHub connection.

Issue #4842 (EPIC #4839 · C3), rulings R1 + R6.

Two stores, both written on every mutation
------------------------------------------
A GitHub connection is recorded in two places, and they are not redundant — they
carry different authority (see ``admin/installations/resolver.py``):

* ``channel_tenant_map`` — written server-side, the only record that can *grant*
  installation ownership. Migration 027 enforces one-installation-one-org on its
  ``installation_id`` column.
* ``organizations.github_installation_ids`` — an assertion a tenant makes about
  itself. On its own it resolves as ``UNATTESTABLE``, never as ownership.

So attach must write BOTH: the map row for routing to work, the column for the
org's own read paths (the identity-index write-through and the onboarding
matcher read it). Detach must clear BOTH, or the leftover half keeps a
half-connection alive — a map row without the column silently keeps routing
webhooks to an org the UI shows as disconnected.

Ordering is deliberate throughout: **validate, then write, then commit, then
side-effect**. The ownership guard runs before any mutation, so a refused claim
leaves the database untouched; the identity-index write is post-commit and
best-effort, so DynamoDB being unavailable cannot roll back a committed binding.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import delete as sa_delete
from sqlalchemy import func as sa_func
from sqlalchemy import select

from src.admin.installations.guards import InstallationClaimError, assert_installation_claimable_by
from src.admin.installations.resolver import OwnerState
from src.shared.models.organization import Organization
from src.shared.models.vault import ChannelTenantMap

from .schemas import (
    GitHubConnectionAttachRequest,
    GitHubConnectionDetachResponse,
    GitHubConnectionListResponse,
    GitHubConnectionResponse,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.admin.identity.identity_index_writer import IdentityIndexWriter

logger = logging.getLogger(__name__)

DETACH_WARNING = (
    "Webhook dispatch for this GitHub organization has stopped. Events from it will no longer "
    "resolve to a tenant and will be refused (fail-closed, by design). Re-attach the installation "
    "to restore routing."
)


class OrganizationNotFoundError(Exception):
    """The target organization does not exist."""


class ConnectionNotFoundError(Exception):
    """The installation is not connected to the target organization."""


class OrgConnectionsService:
    """GitHub-connection lifecycle over the existing org columns (no new table)."""

    def __init__(self, db: AsyncSession, identity_index: IdentityIndexWriter | None = None):
        self._db = db
        # Constructed lazily rather than defaulted in the signature: the writer's
        # constructor builds boto3 clients, so a default argument would make
        # every unit test of this service require AWS credentials.
        self._identity_index = identity_index

    def _writer(self) -> IdentityIndexWriter:
        if self._identity_index is None:
            from src.admin.identity.identity_index_writer import IdentityIndexWriter

            self._identity_index = IdentityIndexWriter()
        return self._identity_index

    async def _get_org(self, org_id: str) -> Organization:
        org = await self._db.get(Organization, org_id)
        if org is None:
            raise OrganizationNotFoundError(f"Organization {org_id} not found")
        return org

    async def list_connections(self, org_id: str) -> GitHubConnectionListResponse:
        """List the org's GitHub connections as the Connections tab shows them.

        Unions both stores rather than reading only the org column, so a binding
        recorded in just one of them is *visible* instead of silently absent —
        that half-state is precisely what an operator needs to see to fix it. It
        is reported via ``routable``, which is False when no ``channel_tenant_map``
        row backs the claim (webhooks for it will not resolve).
        """
        org = await self._get_org(org_id)

        mapped = (
            (
                await self._db.execute(
                    select(ChannelTenantMap).where(
                        ChannelTenantMap.provider == "github",
                        ChannelTenantMap.org_id == org_id,
                        ChannelTenantMap.installation_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        routable_ids = {str(row.installation_id) for row in mapped}
        asserted_ids = {str(i) for i in (org.github_installation_ids or [])}

        connections = [
            GitHubConnectionResponse(
                org_id=org_id,
                installation_id=iid,
                github_org_id=org.github_org_id,
                # Deliberately None: the org's display NAME is not a GitHub
                # login, and per-connection identity is not stored today. An
                # honest null beats a fabricated value the UI would render as
                # if it came from GitHub.
                github_org_login=None,
                routable=iid in routable_ids,
            )
            for iid in sorted(routable_ids | asserted_ids)
        ]
        return GitHubConnectionListResponse(connections=connections, total=len(connections))

    async def attach_github(self, org_id: str, req: GitHubConnectionAttachRequest) -> GitHubConnectionResponse:
        """Bind ``req.installation_id`` to ``org_id``.

        Raises:
            OrganizationNotFoundError: no such org (checked first — binding an
                installation to a nonexistent tenant would create a map row whose
                FK target is missing, i.e. an unroutable orphan).
            InstallationClaimError: the org may not claim this installation. 409
                for a cross-tenant or disputed claim, 403 for one that cannot be
                verified. Raised BEFORE any write.
        """
        org = await self._get_org(org_id)
        installation_id = req.installation_id
        install_id_str = str(installation_id)

        # THE gate. Delegates to ·A0's single resolver via the shared guard, so
        # this writer cannot drift from the two #4072 writers that already use it.
        # Unowned proceeds (this write is the first claim); owned-by-us is an
        # idempotent no-op; owned-by-another, disputed, or unattestable is
        # refused. That is the one-installation-one-org invariant migration 027
        # exists to hold, and it is enforced here rather than left to the unique
        # index so the caller gets a 409 with a message instead of a 500.
        await assert_installation_claimable_by(org_id, installation_id, db=self._db)

        scope_id = req.github_org_id or req.github_org_login or install_id_str

        # Canonical-key lookup FIRST: a retry of the same attach that supplies a
        # different scope hint (login instead of numeric id, or none at all)
        # computes a different scope_id, and a scope-only lookup would miss the
        # row created last time and INSERT a sibling carrying the same
        # installation_id — which migration 027's partial unique index rejects
        # as an unmapped IntegrityError on Postgres and lands silently on
        # SQLite. installation_id is the column the invariant holds unique, so
        # it is the key that makes re-attach idempotent.
        existing = (
            await self._db.execute(
                select(ChannelTenantMap).where(
                    ChannelTenantMap.provider == "github",
                    ChannelTenantMap.installation_id == install_id_str,
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            existing = (
                await self._db.execute(
                    select(ChannelTenantMap).where(
                        ChannelTenantMap.provider == "github",
                        ChannelTenantMap.provider_scope_id == scope_id,
                    )
                )
            ).scalar_one_or_none()

        if existing is not None:
            # The claimability guard keys on the INSTALLATION; this row may have
            # been found by ACCOUNT SCOPE — and a scope row can belong to a
            # different tenant while the requested installation is genuinely
            # unclaimed (a stale or mistyped github_org_id). Overwriting it
            # would re-home the other org's binding in place: their only
            # ownership-granting row silently becomes ours, their webhook
            # routing dies UNATTESTABLE, and the unique index never fires
            # because this is an UPDATE that keeps installation_id unique.
            # Refuse loudly — the same contract as the sibling writer in
            # admin/connections/service.py::_attach_org_installation.
            if existing.org_id != org_id:
                raise InstallationClaimError(
                    f"GitHub account scope '{existing.provider_scope_id}' is already bound to "
                    "another organization; refusing to re-home its installation binding. "
                    "Detach it from its current organization first, or correct the "
                    "github_org_id on this request.",
                    status_code=409,
                    state=OwnerState.RESOLVED,
                    installation_id=installation_id,
                    org_id=org_id,
                )

        old_github_ids = [str(i) for i in (org.github_installation_ids or [])]

        # 1. The org's own assertion.
        if install_id_str not in old_github_ids:
            # Reassigned, not mutated in place: SQLAlchemy does not track
            # in-place mutation of a JSON column, so `.append()` here would
            # commit nothing.
            org.github_installation_ids = old_github_ids + [install_id_str]

        # Recorded when supplied because it is what makes a later ownership check
        # attestable — without it the resolver fails closed as UNATTESTABLE.
        if req.github_org_id:
            org.github_org_id = req.github_org_id

        # 2. The authoritative claim. Upsert on the canonical installation_id
        #    column (#4070 ·A0), keyed by the account scope the other writers use
        #    so all three agree on one row per GitHub account per provider.
        if existing is not None:
            # Ours (the refusal above already ran): an idempotent re-attach, or
            # a re-install carrying a NEW installation id for the same account.
            existing.installation_id = install_id_str
            existing.org_id = org_id
        else:
            self._db.add(
                ChannelTenantMap(
                    provider="github",
                    provider_scope_id=scope_id,
                    installation_id=install_id_str,
                    org_id=org_id,
                )
            )

        await self._db.commit()
        await self._db.refresh(org)

        logger.info(
            "event=org_github_connection_attached org=%s installation_id=%s github_org_id=%s",
            org_id,
            install_id_str,
            req.github_org_id,
        )

        # Post-commit, best-effort: the webhook Lambda routes off the DDB index,
        # so without this the binding is live in Postgres but not yet routable.
        # Failure is logged, never propagated — Postgres is already committed and
        # raising here would report a successful attach as a failure.
        try:
            await self._writer().sync_org_channels(
                org_id=org_id,
                github_installation_ids=[str(i) for i in (org.github_installation_ids or [])],
                cognito_client_ids=[str(c) for c in (org.cognito_client_ids or [])],
                old_github_installation_ids=old_github_ids,
            )
        except Exception:
            logger.exception(
                "event=org_github_connection_index_write_failed org=%s installation_id=%s phase=attach",
                org_id,
                install_id_str,
            )

        return GitHubConnectionResponse(
            org_id=org_id,
            installation_id=install_id_str,
            github_org_id=org.github_org_id,
            github_org_login=req.github_org_login or org.name,
            routable=True,
        )

    async def detach_github(self, org_id: str, installation_id: int) -> GitHubConnectionDetachResponse:
        """Unbind ``installation_id`` from ``org_id``.

        Clears the org's assertion AND deletes the map row(s), then removes the
        identity-index entry. Leaving the index row behind is the stale-routing
        failure mode: the UI would show the org as disconnected while webhooks
        kept resolving to it.

        Raises:
            OrganizationNotFoundError: no such org.
            ConnectionNotFoundError: this installation is not bound to this org.
                Includes the case where it is bound to a DIFFERENT org — reported
                as not-found for this org rather than as a permission error,
                because the operator's request names a connection that does not
                exist here, and confirming another tenant's binding is not this
                route's business.
        """
        org = await self._get_org(org_id)
        install_id_str = str(installation_id)

        # Lock the row before the read-modify-write below. `github_installation_ids`
        # is a JSON list rewritten wholesale, so two concurrent detaches in one org
        # both read the same list, each drops its own id, and the later commit
        # restores the id the earlier one removed — resurrecting a claim for an
        # installation that was just detached. Taken here rather than in
        # `_get_org`, which read-only callers share.
        await self._db.scalar(select(Organization.id).where(Organization.id == org_id).with_for_update())

        old_github_ids = [str(i) for i in (org.github_installation_ids or [])]
        mapped = (
            (
                await self._db.execute(
                    select(ChannelTenantMap).where(
                        ChannelTenantMap.provider == "github",
                        ChannelTenantMap.org_id == org_id,
                        ChannelTenantMap.installation_id == install_id_str,
                    )
                )
            )
            .scalars()
            .all()
        )

        if install_id_str not in old_github_ids and not mapped:
            raise ConnectionNotFoundError(f"Installation {installation_id} is not connected to organization {org_id}")

        # 1. Drop the org's assertion.
        remaining = [i for i in old_github_ids if i != install_id_str]
        org.github_installation_ids = remaining

        # 2. Drop the authoritative claim. Scoped to this org AND this
        #    installation: an unscoped delete by org would also remove the org's
        #    Slack and WhatsApp routing, which this operation never touched.
        if mapped:
            await self._db.execute(
                sa_delete(ChannelTenantMap).where(
                    ChannelTenantMap.provider == "github",
                    ChannelTenantMap.org_id == org_id,
                    ChannelTenantMap.installation_id == install_id_str,
                )
            )

        # 3. Clear the GitHub identity fields once nothing is connected. Left in
        #    place while other installations remain — they are per-account, not
        #    per-installation, and clearing them early would make the survivors
        #    UNATTESTABLE and break their routing.
        #
        #    "Nothing is connected" has to be asked of both stores. A personal
        #    install is never appended to github_installation_ids (install_callback
        #    appends only for account_type == "Organization"), so an org whose
        #    remaining connections are all personal has an empty column and live
        #    map rows. Deciding from the column alone nulls github_org_id out from
        #    under them, which is precisely the UNATTESTABLE breakage this step is
        #    trying to avoid.
        surviving_map_rows = (
            await self._db.execute(
                select(sa_func.count())
                .select_from(ChannelTenantMap)
                .where(
                    ChannelTenantMap.provider == "github",
                    ChannelTenantMap.org_id == org_id,
                    ChannelTenantMap.installation_id != install_id_str,
                )
            )
        ).scalar_one()

        if not remaining and not surviving_map_rows:
            org.github_org_id = None
            org.github_app_id = None

        await self._db.commit()
        await self._db.refresh(org)

        logger.warning(
            "event=org_github_connection_detached org=%s installation_id=%s remaining=%d outcome=webhook_dispatch_stopped_for_this_installation",
            org_id,
            install_id_str,
            len(remaining),
        )

        # Post-commit, best-effort: remove the index row so webhook resolution
        # fails closed. Passing the old list as `old_github_installation_ids`
        # makes sync_org_channels delete exactly the ids that disappeared.
        try:
            await self._writer().sync_org_channels(
                org_id=org_id,
                github_installation_ids=remaining,
                cognito_client_ids=[str(c) for c in (org.cognito_client_ids or [])],
                old_github_installation_ids=old_github_ids,
            )
        except Exception:
            logger.exception(
                "event=org_github_connection_index_write_failed org=%s installation_id=%s phase=detach",
                org_id,
                install_id_str,
            )

        return GitHubConnectionDetachResponse(
            detached=True,
            org_id=org_id,
            installation_id=install_id_str,
            warning=DETACH_WARNING,
        )
