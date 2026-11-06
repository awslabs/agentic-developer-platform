"""Refresh a signed tools principal against current human memberships."""

from fastapi import HTTPException
from sqlalchemy import select

from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import TeamMembership, User


async def current_tools_human(db, principal):
    db.expire_all()
    user = await db.scalar(
        select(User)
        .join(TenantMembership, TenantMembership.user_id == User.id)
        .join(TeamMembership, TeamMembership.user_id == User.id)
        .where(
            User.id == principal["user_id"],
            User.org_id == principal["tenant_id"],
            User.team_id == principal["team_id"],
            User.user_kind == "human",
            User.is_shadow.is_(False),
            TenantMembership.tenant_id == principal["tenant_id"],
            TenantMembership.is_active.is_(True),
            TenantMembership.revoked_at.is_(None),
            TeamMembership.org_id == principal["tenant_id"],
            TeamMembership.team_id == principal["team_id"],
        )
        .with_for_update(read=True, of=(User, TenantMembership, TeamMembership))
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise HTTPException(403, "current human membership required")
    return user
