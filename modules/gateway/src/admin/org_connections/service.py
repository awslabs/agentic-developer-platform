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

from sqlalchemy import select

from src.admin.installations.guards import InstallationClaimError, assert_installation_claimable_by, lock_installation_organization
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


class RoutingReconciliationRefusedError(Exception):
    def __init__(self, reason: str, status_code: int = 409, observed_org_id: str | None = None, authoritative_org_id: str | None = None):
        self.reason = reason
        self.status_code = status_code
        self.observed_org_id = observed_org_id
        self.authoritative_org_id = authoritative_org_id
        super().__init__(reason)


async def reconcile_routing(org_id: str, installation_id: int, expected_org_id: str, db: AsyncSession) -> tuple[str, str]:
    """Attest canonical ownership before touching a single forward projection."""
    from src.admin.audit_operation import mark_admin_effects
    from src.admin.connections.github_client import GitHubAppClient
    from src.admin.connections.service import _get_github_app_credentials
    from src.admin.identity_index import IdentityIndexClient
    from src.admin.installations.resolver import OwnerState, resolve_installation_owner

    if installation_id <= 0:
        raise RoutingReconciliationRefusedError("invalid_installation_id", 422)
    try:
        app_id, private_key = _get_github_app_credentials()
        if not app_id or not private_key:
            raise RoutingReconciliationRefusedError("github_app_credentials_unavailable", 503)
        client = GitHubAppClient(app_id, private_key)
        try:
            owner, state = await resolve_installation_owner(installation_id, db=db, attest=True, github_client=client)
        finally:
            await client.aclose()
    except RoutingReconciliationRefusedError:
        raise
    except Exception as exc:
        logger.warning("Routing reconciliation attestation unavailable for installation=%s: %s", installation_id, type(exc).__name__)
        raise RoutingReconciliationRefusedError("github_attestation_unavailable", 503) from exc

    if state == OwnerState.REVOKED:
        raise RoutingReconciliationRefusedError("installation_revoked")
    if state != OwnerState.RESOLVED or owner is None or not owner.attested:
        raise RoutingReconciliationRefusedError(f"ownership_{state.value}", 403)
    if owner.tenant_id != org_id:
        raise RoutingReconciliationRefusedError("canonical_owner_differs_from_target", authoritative_org_id=owner.tenant_id)

    mark_admin_effects()
    try:
        outcome, observed = await IdentityIndexClient().reconcile_installation_routing(installation_id, expected_org_id, org_id)
    except Exception as exc:
        logger.warning("Routing reconciliation projection unavailable for installation=%s: %s", installation_id, type(exc).__name__)
        raise RoutingReconciliationRefusedError("projection_unavailable", 503, authoritative_org_id=org_id) from exc
    if outcome not in {"repaired", "already_consistent"}:
        raise RoutingReconciliationRefusedError(outcome, observed_org_id=observed, authoritative_org_id=org_id)
    return outcome, observed or org_id


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
        org = await lock_installation_organization(self._db, org_id)
        if org is None:
            raise OrganizationNotFoundError(f"Organization {org_id} not found")
        installation_id = req.installation_id
        install_id_str = str(installation_id)

        if req.restore_revoked:
            from src.admin.identity_index import IdentityIndexClient
            from src.shared.models.base import utcnow
            from src.shared.models.vault import InstallationRevocation

            record = await self._db.scalar(
                select(InstallationRevocation)
                .where(InstallationRevocation.installation_id == install_id_str)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if record is None or record.org_id != org_id or record.provider_uninstall_requested or record.cleanup_pending:
                raise InstallationClaimError(
                    "Only a completed local detach owned by this tenant can be explicitly restored",
                    status_code=409,
                    state=OwnerState.REVOKED,
                    installation_id=installation_id,
                    org_id=org_id,
                )
            # The route is platform-admin-only. Keep SQL denial in place until
            # the marker is cleared and the new canonical claim commits. A crash
            # anywhere before that commit still denies, including during outage.
            if not await IdentityIndexClient().clear_installation_revocation(install_id_str, org_id):
                raise InstallationClaimError(
                    "Restoration could not clear the denial projection; retry this explicit restore",
                    status_code=409,
                    state=OwnerState.REVOKED,
                    installation_id=installation_id,
                    org_id=org_id,
                )
            record.restored_at = utcnow()
            await self._db.flush()

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
        """Revoke local routing without requesting a provider uninstall."""
        from src.admin.connections.service import _cache_invalidate, _invalidate_verification_cache, _repo_cache_invalidate
        from src.admin.installations.revocation import revoke_installation

        await self._get_org(org_id)
        try:
            result = await revoke_installation(
                installation_id=installation_id,
                org_id=org_id,
                db=self._db,
                user_id=None,
                is_admin=True,
                uninstall=False,
                index=self._identity_index._client if self._identity_index is not None else None,
            )
        except (ValueError, PermissionError) as exc:
            raise ConnectionNotFoundError(str(exc)) from exc
        _cache_invalidate(installation_id)
        _repo_cache_invalidate(installation_id)
        _invalidate_verification_cache()
        return GitHubConnectionDetachResponse(
            detached=True,
            org_id=org_id,
            installation_id=str(installation_id),
            warning=result.warning or "Local access is revoked. The installation remains at GitHub.",
            residual=result.residual,
        )
