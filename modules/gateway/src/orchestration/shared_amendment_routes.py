"""Human-owned append amendments; no policy, worker, or budget reset inputs."""

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
from .shared_amendment import SharedAppendError, SharedAppendRequest, accept_shared_append, preview_shared_append

router = APIRouter()


async def call(*, accept, flow_id, body, current_user, access, db):
    from src.agentauth.bootstrap import BootstrapRefusedError
    from src.agentauth.human_control import authorize_human_session

    from .routes import _resolve_actor_role

    try:
        human = await authorize_human_session(current_user, db)
    except BootstrapRefusedError:
        raise HTTPException(403, "An authenticated human plan approver is required.") from None
    actor = ApprovalContext(
        org_id=human.tenant_id,
        actor_id=human.user_id,
        actor_role=await _resolve_actor_role(access, current_user),
        reason=body.reason,
    )
    try:
        result = await (accept_shared_append if accept else preview_shared_append)(db, flow_id=flow_id, actor=actor, request=body)
        if accept:
            await db.commit()
        return result
    except (SharedAppendError, CycleBlockedError) as error:
        await db.rollback()
        code = error.code if isinstance(error, SharedAppendError) else error.reason
        raise HTTPException(409, {"code": code, "retryable": code == "amendment_dispatch_in_progress"}) from None
    except ValueError as error:
        await db.rollback()
        raise HTTPException(422, str(error)) from None


@router.post("/flows/{flow_id}/append/preview")
async def preview_append(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: SharedAppendRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    access = AccessControl(db)
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    return await call(accept=False, flow_id=flow_id, body=body, current_user=current_user, access=access, db=db)


@router.post("/flows/{flow_id}/append/accept")
async def accept_append(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: SharedAppendRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    access = AccessControl(db)
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    return await call(accept=True, flow_id=flow_id, body=body, current_user=current_user, access=access, db=db)
