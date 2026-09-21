"""Human-owned live budget controls. No worker may change financial enforcement."""

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.human_control import authorize_human_session
from src.auth.dependencies import get_current_user
from src.orchestration.models import OrchestrationFlow
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.base import utcnow
from src.shared.schemas.auth import TokenContext

from .enforcement_settings import BudgetEnforcementSetting, flow_key, read_enforcement

router = APIRouter(prefix="/budget/enforcement", tags=["budgets"])
User = Annotated[TokenContext, Depends(get_current_user)]
DB = Annotated[AsyncSession, Depends(get_db)]
FlowID = Annotated[str, Path(min_length=1, max_length=36)]


class EnforcementUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    enabled: bool
    expected_revision: int = Field(ge=0)
    reason: str = Field(min_length=3, max_length=1000)


async def visible_flow(db, user, flow_id):
    flow = await db.scalar(select(OrchestrationFlow).where(OrchestrationFlow.id == flow_id, OrchestrationFlow.org_id == user.org_id))
    if flow is None:
        raise HTTPException(404, "Flow not found")


async def status(db, user, flow_id=None):
    posture = await read_enforcement(db, org_id=user.org_id, flow_id=flow_id)
    return {
        **asdict(posture),
        "effective_enabled": posture.enabled,
        "flow_id": flow_id,
        "revision": posture.flow_revision if flow_id else posture.global_revision,
    }


async def change(db, user, body, flow_id=None):
    access = AccessControl(db)
    access.require_platform_admin(user)
    await access.check_permission(user, Permission.BUDGET_UPDATE, target_org_id=user.org_id)
    if flow_id:
        await access.check_permission(user, Permission.PLAN_APPROVE, target_org_id=user.org_id)
    try:
        human = await authorize_human_session(user, db)
    except BootstrapRefusedError:
        raise HTTPException(403, "A current authenticated human administrator is required") from None
    if flow_id:
        await visible_flow(db, user, flow_id)
    key = flow_key(user.org_id, flow_id) if flow_id else "global"
    before = await read_enforcement(db, org_id=user.org_id, flow_id=flow_id)
    values = {"enabled": body.enabled, "revision": body.expected_revision + 1, "updated_by": human.user_id, "updated_at": utcnow()}
    try:
        if body.expected_revision == 0:
            db.add(BudgetEnforcementSetting(scope_key=key, **values))
            await db.flush()
        else:
            result = await db.execute(
                update(BudgetEnforcementSetting)
                .where(
                    BudgetEnforcementSetting.scope_key == key,
                    BudgetEnforcementSetting.revision == body.expected_revision,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise HTTPException(409, "Budget setting changed. Refresh before saving.")
        db.add(
            AuditLog(
                org_id=user.org_id,
                actor_id=human.user_id,
                event_type="budget_enforcement_changed",
                details={
                    "scope_key": key,
                    "flow_id": flow_id,
                    "enabled": body.enabled,
                    "previous_enabled": before.flow_enabled if flow_id else before.global_enabled,
                    "revision": values["revision"],
                    "reason": body.reason,
                },
            )
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Budget setting changed. Refresh before saving.") from None
    return await status(db, user, flow_id)


@router.get("")
async def get_global(user: User, db: DB):
    await AccessControl(db).check_permission(user, Permission.BUDGET_READ, target_org_id=user.org_id)
    return await status(db, user)


@router.post("")
async def set_global(body: EnforcementUpdate, user: User, db: DB):
    return await change(db, user, body)


@router.get("/flows/{flow_id}")
async def get_flow(flow_id: FlowID, user: User, db: DB):
    await AccessControl(db).check_permission(user, Permission.BUDGET_READ, target_org_id=user.org_id)
    await visible_flow(db, user, flow_id)
    return await status(db, user, flow_id)


@router.post("/flows/{flow_id}")
async def set_flow(flow_id: FlowID, body: EnforcementUpdate, user: User, db: DB):
    return await change(db, user, body, flow_id)
