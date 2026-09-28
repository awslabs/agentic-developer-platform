"""Human-only preview and acceptance of one unexecuted evaluation exception."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from .compile import ApprovalContext
from .evaluation_waiver import WaiverError, WaiverRequest, accept_waiver, preview_waiver
from .review_cycle import CycleBlockedError

router = APIRouter()


async def operate(function, flow_id, request, current_user, db, access):
    from src.agentauth.bootstrap import BootstrapRefusedError
    from src.agentauth.human_control import authorize_human_session

    from .routes import _resolve_actor_role

    try:
        human = await authorize_human_session(current_user, db)
    except BootstrapRefusedError:
        raise HTTPException(403, "An authenticated human plan approver is required.") from None
    actor = ApprovalContext(
        org_id=human.tenant_id, actor_id=human.user_id, actor_role=await _resolve_actor_role(access, current_user), reason=request.reason
    )
    try:
        result = await function(db, flow_id=flow_id, actor=actor, request=request)
        if function is accept_waiver:
            await db.commit()
        return result
    except (WaiverError, CycleBlockedError) as error:
        await db.rollback()
        code = error.code if isinstance(error, WaiverError) else error.reason
        raise HTTPException(409, {"code": code, "retryable": code == "evaluation_dispatch_in_progress"}) from None


@router.post("/flows/{flow_id}/evaluation-waiver/preview")
async def preview(flow_id: str, request: WaiverRequest, current_user: TokenContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    access = AccessControl(db)
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    return await operate(preview_waiver, flow_id, request, current_user, db, access)


@router.post("/flows/{flow_id}/evaluation-waiver/accept")
async def accept(flow_id: str, request: WaiverRequest, current_user: TokenContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    access = AccessControl(db)
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    return await operate(accept_waiver, flow_id, request, current_user, db, access)
