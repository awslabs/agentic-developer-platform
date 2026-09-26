"""Durable tenant membership removal; never delete a person's identity or login."""

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import MultipleResultsFound

from src.admin.exceptions import ResourceConflictError, ResourceNotFoundError
from src.admin.memberships import project_member_org_ids
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import TeamMembership, User


async def revoke_membership(db, *, org_id, user_id):
    user = await db.scalar(select(User).where(User.id == user_id, User.org_id == org_id).with_for_update())
    if user is None:
        raise ResourceNotFoundError("User", user_id)
    member = await db.scalar(
        select(TenantMembership).where(TenantMembership.user_id == user_id, TenantMembership.tenant_id == org_id).with_for_update()
    )
    if member is None:
        # Native legacy membership has no authority row. The tombstone must exist
        # even here, otherwise workspace fallback would recreate member access.
        member = TenantMembership(user_id=user_id, tenant_id=org_id, role="member", is_active=False, joined_via="admin_remove")
        db.add(member)
    member.revoked_at = member.revoked_at or datetime.now(UTC)
    member.is_active = False
    await db.execute(delete(TeamMembership).where(TeamMembership.user_id == user_id, TeamMembership.org_id == org_id))
    user.team_id = ""
    await db.commit()
    await project_member_org_ids(db, user_id=user_id)
    return member


async def reactivate_membership(db, *, org_id, user_id, role):
    """Only explicit authorized placement may clear a removal tombstone."""
    member = await db.scalar(
        select(TenantMembership).where(TenantMembership.user_id == user_id, TenantMembership.tenant_id == org_id).with_for_update()
    )
    if member is not None and member.revoked_at is not None:
        member.revoked_at = None
        member.role = role
        member.is_active = False
        await db.flush()


def require_not_revoked(member):
    if member is not None and member.revoked_at is not None:
        raise ResourceConflictError("TenantMembership", "revoked", "Explicit authorized member add is required before updating this membership")


async def is_revoked(db, *, subject, org_id, username=""):
    from src.shared.identity.workspaces import linked_user_ids, login_user

    user = await login_user(db, subject)
    if user is None:
        return False  # Preserve pre-existing provisioning behavior.
    ids = await linked_user_ids(db, user, username=username)
    return bool(
        await db.scalar(
            select(TenantMembership.id)
            .where(TenantMembership.user_id.in_(ids), TenantMembership.tenant_id == org_id, TenantMembership.revoked_at.is_not(None))
            .limit(1)
        )
    )


async def require_not_revoked_context(context, db=None):
    from fastapi import HTTPException

    from src.shared.database import get_session_factory

    if context.account_type == "service" or context.is_admin or not context.org_id:
        return
    if db is None:
        async with get_session_factory()() as session:
            return await require_not_revoked_context(context, session)
    try:
        revoked = await is_revoked(db, subject=context.user_id, org_id=context.org_id, username=context.cognito_username)
    except MultipleResultsFound:
        raise HTTPException(403, {"error": "ambiguous_identity", "message": "The login does not resolve to one canonical identity."}) from None
    if revoked:
        raise HTTPException(
            403, {"error": "tenant_membership_revoked", "message": "This organization membership was removed; select another authorized workspace."}
        )
