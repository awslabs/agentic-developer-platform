"""Local installation revocation with durable authorization for cleanup retries."""

import logging

from sqlalchemy import select

from src.admin.connections.schemas import DeleteConnectionResponse
from src.admin.identity_index import IdentityIndexClient
from src.admin.installations.guards import lock_installation_organization
from src.admin.installations.resolver import OwnerState, resolve_installation_owner
from src.shared.models.vault import ChannelTenantMap, InstallationRevocation

logger = logging.getLogger(__name__)
PROJECTIONS = ["identity_index_denial", "identity_index_forward_row", "identity_index_reverse_row"]


def names_installation(row: ChannelTenantMap, installation_id: str) -> bool:
    """Legacy metadata is usable for scoped cleanup, never account-ID guessing."""
    return row.installation_id == installation_id or (
        row.installation_id is None and str((row.install_metadata or {}).get("installation_id", "")) == installation_id
    )


def _authorize(record: InstallationRevocation, org_id: str, user_id: str | None, is_admin: bool) -> None:
    if record.org_id != org_id or not (is_admin or (user_id and user_id in record.authorized_user_ids)):
        raise PermissionError("Only the owning tenant's authorized installer or platform administrator may retry this revocation")


async def revoke_installation(
    *,
    installation_id: int,
    org_id: str,
    db,
    user_id: str | None,
    is_admin: bool,
    uninstall: bool,
    github_client=None,
    index: IdentityIndexClient | None = None,
) -> DeleteConnectionResponse:
    """Commit denial first. Failed provider/index work remains resumable after restart."""
    iid = str(installation_id)
    # Serialize initial creation in a tenant; later attempts also lock the durable
    # operation row. A revoked ID cannot be claimed by another tenant.
    org = await lock_installation_organization(db, org_id)
    if org is None:
        raise PermissionError("Installation belongs to a different ADP tenant")
    record = await db.scalar(
        select(InstallationRevocation)
        .where(InstallationRevocation.installation_id == iid)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if record is not None and record.restored_at is None:
        _authorize(record, org_id, user_id, is_admin)
        # Retry only the recorded intent. Calling the uninstall endpoint after a
        # local detach never upgrades that operation into a provider DELETE.
    else:
        rows = list(
            (await db.scalars(select(ChannelTenantMap).where(ChannelTenantMap.provider == "github", ChannelTenantMap.org_id == org_id))).all()
        )
        mapped = [row for row in rows if names_installation(row, iid)]
        asserted = iid in [str(value) for value in (org.github_installation_ids or [])]
        installers = sorted({row.installed_by_user_id for row in mapped if row.installed_by_user_id})
        if not mapped and not asserted:
            owner, state = await resolve_installation_owner(installation_id, db=db)
            if owner is not None and owner.tenant_id != org_id:
                raise PermissionError("Installation belongs to a different ADP tenant")
            raise ValueError(f"Installation {iid} is not connected to this tenant")
        if not (is_admin or (user_id and user_id in installers)):
            raise PermissionError(
                "You do not have permission to disconnect this installation; only its recorded installer or a platform administrator may do so"
            )
        owner, state = await resolve_installation_owner(installation_id, db=db)
        if state is OwnerState.AMBIGUOUS and any(row.installation_id == iid for row in mapped):
            raise PermissionError("Installation is claimed by more than one ADP tenant and is quarantined; resolve the conflict first")
        corroborated = state is OwnerState.RESOLVED and owner is not None and owner.tenant_id == org_id
        # A tenant may retract its own unproven claim, even if another tenant owns
        # the installation. That does NOT authorize a global tombstone or DELETE
        # at GitHub. This also safely handles legacy metadata-only records.
        if not corroborated:
            if not is_admin:
                raise PermissionError("Unproven or disputed installation claims require operator-local cleanup")
            for row in mapped:
                await db.delete(row)
            org.github_installation_ids = [str(value) for value in (org.github_installation_ids or []) if str(value) != iid]
            await db.commit()
            return DeleteConnectionResponse(
                deleted=True,
                installation_id=installation_id,
                local_revoked=True,
                provider_revoked=False,
                warning="The local claim was removed. No provider uninstall or global revocation was authorized.",
            )

        if record is None:
            record = InstallationRevocation(installation_id=iid, org_id=org_id)
            db.add(record)
        record.authorized_user_ids = installers
        record.provider_uninstall_requested = uninstall
        record.provider_revoked = False
        record.restored_at = None
        record.cleanup_pending = list(PROJECTIONS)
        for row in mapped:
            await db.delete(row)
        remaining_maps = [row for row in rows if row not in mapped]
        org.github_installation_ids = [str(value) for value in (org.github_installation_ids or []) if str(value) != iid]
        if not org.github_installation_ids and not remaining_maps:
            org.github_org_id = None
            org.github_app_id = None
        # Once committed, every canonical installation lookup denies, even if a
        # subsequent DDB marker or provider request fails or this process stops.
        await db.commit()

    return await _complete_revocation(
        installation_id=installation_id,
        org_id=org_id,
        db=db,
        user_id=user_id,
        is_admin=is_admin,
        github_client=github_client,
        index=index,
    )


async def _complete_revocation(*, installation_id, org_id, db, user_id, is_admin, github_client, index):
    iid = str(installation_id)
    # Lock again after the denial commit. Restore and other retries cannot
    # interleave provider/cleanup work with this attempt.
    record = await db.scalar(
        select(InstallationRevocation)
        .where(InstallationRevocation.installation_id == iid)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    _authorize(record, org_id, user_id, is_admin)
    if record.restored_at is not None:
        raise PermissionError("This installation has been explicitly restored; an old cleanup cannot revoke it")
    if index is None:
        try:
            index = IdentityIndexClient()
        except Exception:
            logger.exception("Installation revocation index client is unavailable: installation=%s", iid)
    residual = []
    for name, operation in (
        ("identity_index_denial", lambda: index.put_installation_revocation(iid, org_id)),
        ("identity_index_forward_row", lambda: index.delete_installation_projection(iid, org_id)),
        ("identity_index_reverse_row", lambda: index.delete_reverse_installation_if_matches(org_id, iid)),
    ):
        try:
            if not await operation():
                residual.append(name)
        except Exception:
            logger.exception("Installation revocation cleanup failed: installation=%s phase=%s", iid, name)
            residual.append(name)
    record.cleanup_pending = residual
    if record.provider_uninstall_requested and not record.provider_revoked:
        try:
            if github_client is None:
                raise RuntimeError("GitHub App credentials are unavailable")
            await github_client.delete_installation(installation_id)
            record.provider_revoked = True
        except Exception:
            logger.exception("Provider uninstall remains pending for locally revoked installation=%s", iid)
    await db.commit()
    provider_pending = record.provider_uninstall_requested and not record.provider_revoked
    pending = [*residual, *(["provider_uninstall"] if provider_pending else [])]
    return DeleteConnectionResponse(
        deleted=True,
        local_revoked=True,
        installation_id=installation_id,
        provider_revoked=record.provider_revoked,
        provider_uninstall_requested=record.provider_uninstall_requested,
        residual=pending,
        warning="Local access is revoked. Retry to finish the pending cleanup or provider uninstall." if pending else None,
    )
