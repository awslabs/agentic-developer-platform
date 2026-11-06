"""Authenticated owner acceptance of externally verified operations work."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from .compile import ApprovalContext
from .external_completion import ExternalCompletionError, ExternalCompletionRequest, complete_external
from .review_cycle import CycleBlockedError

router = APIRouter()


async def call(*, accept, flow_id, body, current_user, db, access):
    from src.agentauth.bootstrap import BootstrapRefusedError
    from src.agentauth.human_control import authorize_human_session

    from .routes import _resolve_actor_role

    try:
        human = await authorize_human_session(current_user, db)
    except BootstrapRefusedError:
        raise HTTPException(403, "An authenticated plan approver is required.") from None
    actor = ApprovalContext(
        org_id=human.tenant_id, actor_id=human.user_id, actor_role=await _resolve_actor_role(access, current_user), reason=body.reason
    )
    try:
        result = await complete_external(db, flow_id=flow_id, actor=actor, request=body, accept=accept)
        if accept:
            await db.commit()
        return result
    except (ExternalCompletionError, CycleBlockedError) as error:
        await db.rollback()
        raise HTTPException(409, {"code": str(error)}) from None


@router.post("/flows/{flow_id}/external-completion/preview")
async def preview(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: ExternalCompletionRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    access = AccessControl(db)
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    return await call(accept=False, flow_id=flow_id, body=body, current_user=current_user, db=db, access=access)


@router.post("/flows/{flow_id}/external-completion/accept")
async def accept(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: ExternalCompletionRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    access = AccessControl(db)
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    return await call(accept=True, flow_id=flow_id, body=body, current_user=current_user, db=db, access=access)
