"""Operator-only preview/save of inert registered drafts (#5331)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from .amend import FlowNotFoundError
from .compile import ApprovalContext, ProposalRejectedError
from .draft_revision import DraftRevisionConflictError, DraftRevisionRequest, SaveDraftRevisionRequest, preview_draft_revision, save_draft_revision
from .state import ActorKind

router = APIRouter()


async def get_access_control(db: Annotated[AsyncSession, Depends(get_db)]) -> AccessControl:
    return AccessControl(db)


async def _operate(operation, db, flow_id, request, current_user, access):
    # PLAN_APPROVE alone does not turn a registry service into a human operator.
    if current_user.account_type != "human":
        raise HTTPException(status_code=403, detail={"error": "draft_revision_operator_required", "message": "A human operator is required."})
    role = (await access.get_user_role(current_user))[0]
    actor = ApprovalContext(org_id=current_user.org_id, actor_id=current_user.user_id, actor_role=role.value, actor_kind=ActorKind.HUMAN)
    try:
        return await operation(db, flow_id, request, actor)
    except FlowNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"error": "flow_not_found", "message": str(exc)}) from None
    except DraftRevisionConflictError as exc:
        raise HTTPException(status_code=409, detail={"error": exc.code, "message": str(exc)}) from None
    except ProposalRejectedError as exc:
        raise HTTPException(
            status_code=422, detail={"error": "invalid_draft_revision", "message": str(exc), "violations": [str(v) for v in exc.violations]}
        ) from None


@router.post("/flows/{flow_id}/draft/preview")
async def preview_revision(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    request: DraftRevisionRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    return await _operate(preview_draft_revision, db, flow_id, request, current_user, access)


@router.post("/flows/{flow_id}/draft/revise")
async def save_revision(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    request: SaveDraftRevisionRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    result = await _operate(save_draft_revision, db, flow_id, request, current_user, access)
    await db.commit()
    return result
