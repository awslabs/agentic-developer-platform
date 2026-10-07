"""Human plan and budget authority for live flow financial increases."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from .compile import ApprovalContext
from .review_cycle import CycleBlockedError
from .shared_amendment import SharedAppendError
from .shared_budget import BudgetIncreaseError, BudgetIncreaseRequest, accept_budget_increase, preview_budget_increase

router = APIRouter()


async def call(*, accept, flow_id, body, current_user, access, db):
    from src.agentauth.bootstrap import BootstrapRefusedError
    from src.agentauth.human_control import authorize_human_session

    from .routes import _resolve_actor_role

    try:
        human = await authorize_human_session(current_user, db)
    except BootstrapRefusedError:
        raise HTTPException(403, "An authenticated human budget approver is required.") from None
    actor = ApprovalContext(
        org_id=human.tenant_id, actor_id=human.user_id, actor_role=await _resolve_actor_role(access, current_user), reason=body.reason
    )
    try:
        result = await (accept_budget_increase if accept else preview_budget_increase)(db, flow_id=flow_id, actor=actor, request=body)
        if accept:
            await db.commit()
        return result
    except (BudgetIncreaseError, SharedAppendError, CycleBlockedError) as error:
        await db.rollback()
        code = error.reason if isinstance(error, CycleBlockedError) else str(error)
        raise HTTPException(409, {"code": code}) from None


@router.post("/flows/{flow_id}/budget/preview")
async def preview_budget(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: BudgetIncreaseRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    access = AccessControl(db)
    # A flow supplement can raise platform run/chain ceilings. Tenant budget
    # permissions alone cannot grant that authority.
    access.require_platform_admin(current_user)
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    await access.check_permission(current_user, Permission.BUDGET_UPDATE, target_org_id=current_user.org_id)
    return await call(accept=False, flow_id=flow_id, body=body, current_user=current_user, access=access, db=db)


@router.post("/flows/{flow_id}/budget/accept")
async def accept_budget(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: BudgetIncreaseRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    access = AccessControl(db)
    # A flow supplement can raise platform run/chain ceilings. Tenant budget
    # permissions alone cannot grant that authority.
    access.require_platform_admin(current_user)
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    await access.check_permission(current_user, Permission.BUDGET_UPDATE, target_org_id=current_user.org_id)
    return await call(accept=True, flow_id=flow_id, body=body, current_user=current_user, access=access, db=db)
