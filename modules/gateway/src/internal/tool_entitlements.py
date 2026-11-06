"""Resolve tenant grants before optional team restrictions (missing tenant grants deny)."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.tool_entitlement import ToolEntitlement


async def effective_entitlement(db: AsyncSession, *, tenant_id: str, team_id: str, tool_name: str) -> tuple[bool, Decimal | None]:
    grants = (
        await db.scalars(
            select(ToolEntitlement).where(
                ToolEntitlement.org_id == tenant_id,
                ToolEntitlement.team_id.in_(["", team_id]),
                ToolEntitlement.tool_name == tool_name,
            )
        )
    ).all()
    tenant = next((grant for grant in grants if grant.team_id == ""), None)
    if tenant is None or not tenant.enabled:
        return False, None
    team = next((grant for grant in grants if grant.team_id == team_id), None)
    if team is not None and not team.enabled:
        return False, None
    ceilings = [grant.max_cost_usd for grant in (tenant, team) if grant is not None and grant.max_cost_usd is not None]
    return True, min(ceilings) if ceilings else None


async def entitled_tools(db, *, tenant_id, team_id, names) -> list[str]:
    """The subset of ``names`` the tenant entitles for ``team_id`` (one query, same rule as above).

    Used for the first-party platform tools (#8113), whose per-user allow is implicit: an
    enabled tenant row that no team row disables is the whole entitlement question.
    """
    names = sorted(set(names))
    if not names:
        return []
    grants = (
        await db.scalars(
            select(ToolEntitlement).where(
                ToolEntitlement.org_id == tenant_id,
                ToolEntitlement.team_id.in_(["", team_id] if team_id else [""]),
                ToolEntitlement.tool_name.in_(names),
            )
        )
    ).all()
    tenant = {grant.tool_name: grant for grant in grants if grant.team_id == ""}
    team = {grant.tool_name: grant for grant in grants if team_id and grant.team_id == team_id}
    return [
        name
        for name in names
        if (tenant_grant := tenant.get(name)) is not None
        and tenant_grant.enabled
        and not ((team_grant := team.get(name)) is not None and not team_grant.enabled)
    ]
