"""Authorize legacy budget reads before exposing individual targets (#5668)."""

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import AdminRole, Permission
from src.budget.enforcement_service import _INFRASTRUCTURE_FAULTS
from src.shared.identity.workspaces import workspace_user
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EntityType

_DENIAL = "Not authorized to read budget data for the requested scope."


@dataclass
class BudgetReadScope:
    db: AsyncSession
    context: TokenContext
    access: AccessControl
    role: AdminRole
    owner_ids: frozenset[str]

    async def allows(self, entity_type: str, entity_id: str) -> bool:
        if not entity_id or not entity_id.strip():
            return False
        try:
            EntityType(entity_type)
        except ValueError:
            return False
        if entity_type == EntityType.ORGANIZATION.value:
            return entity_id == self.context.org_id  # Organization totals contain no individual breakdown.
        if entity_type in (EntityType.USER.value, EntityType.ROOT_USER.value) and entity_id in self.owner_ids:
            return True
        if entity_type == EntityType.SERVICE_ACCOUNT.value and self.context.account_type == "service" and entity_id == self.context.user_id:
            return True
        if self.role in (AdminRole.PLATFORM_ADMIN, AdminRole.ORG_ADMIN, AdminRole.DEPT_ADMIN):
            from src.budget.managed_scope_routes import _authorize_scope

            if entity_type in (EntityType.USER.value, EntityType.ROOT_USER.value, EntityType.TEAM.value, EntityType.DEPARTMENT.value):
                try:
                    org_id, _ = await _authorize_scope(self.db, self.access, self.context, entity_type, entity_id)
                    return org_id == self.context.org_id
                except HTTPException as exc:
                    if exc.status_code != 403:
                        raise
                return False
            if self.role == AdminRole.DEPT_ADMIN:
                return False  # No authoritative department edge for a run/service/flow target.
            await self.access.check_permission(self.context, Permission.BUDGET_READ, target_org_id=self.context.org_id)
            # Non-person targets have no common ownership model. Require a stored
            # budget or usage record in this tenant before authorizing their read.
            for model in (BudgetConfig, BudgetUsage):
                stored = await self.db.scalar(
                    select(model.id)
                    .where(
                        model.org_id == self.context.org_id,
                        model.entity_type == entity_type,
                        model.entity_id == entity_id,
                    )
                    .limit(1)
                )
                if stored is not None:
                    return True
        return False

    async def require(self, entity_type: str, entity_id: str) -> None:
        if not await self.allows(entity_type, entity_id):
            raise HTTPException(status_code=403, detail=_DENIAL)


async def budget_read_scope(db: AsyncSession, context: TokenContext) -> BudgetReadScope:
    if not context.org_id or not context.org_id.strip():
        raise HTTPException(status_code=403, detail=_DENIAL)
    access = AccessControl(db)
    await access.check_permission(context, Permission.USAGE_READ, target_org_id=context.org_id)
    role, _, _ = await access.get_user_role(context)
    try:
        owner = await workspace_user(db, context.user_id, context.org_id, username=context.cognito_username)
    except (*_INFRASTRUCTURE_FAULTS, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Budget authorization is temporarily unavailable") from exc
    owner_ids = {context.user_id}
    if owner is not None:
        owner_ids.add(owner.id)
        if owner.cognito_sub:
            owner_ids.add(owner.cognito_sub)
    return BudgetReadScope(db, context, access, role, frozenset(owner_ids))
