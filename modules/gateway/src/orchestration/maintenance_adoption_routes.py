"""Human owner approval of an exact, reviewed maintenance release amendment."""

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from .compile import ApprovalContext, ProposalRejectedError
from .maintenance_adoption import MaintenanceAdoptionRequest, adopt_maintenance
from .review_cycle import CycleBlockedError
from .shared_amendment import SharedAppendError

router = APIRouter()

RECOVERY_ACTIONS = {
    "maintenance_squash_tree_mismatch": "The reviewed repair head and its reported squash merge have different file trees. "
    "Reconcile the missing or changed code; do not bypass publication-history verification.",
    "maintenance_squash_merge_changed": "Read the actual merged repair PR and supply its exact final head and squash merge in a fresh preview.",
    "maintenance_squash_publication_mismatch": "Match every declared repair head to its original recorded PR receipt; "
    "preserve all historical publications.",
    "maintenance_preview_changed": "The plan, execution history, reviewed source, checks, or role changed. "
    "Read a fresh preview and review its differences before accepting.",
    "maintenance_policy_expired": "Renew the original flow's execution window with its owner, then preview maintenance adoption again.",
    "maintenance_wall_clock_elapsed": "Increase the elapsed-time ceiling through the existing owner window renewal; retained flow age is not reset.",
    "maintenance_execution_outstanding": "Reconcile the outstanding execution and its provider effects before adopting maintenance. "
    "Do not start a duplicate run.",
    "maintenance_claim_not_settled": "Reconcile the existing worker exit and work claim before adopting maintenance.",
    "maintenance_checks_unverified": "Wait for every reviewed required check to pass on the exact PR head, then obtain a fresh preview.",
    "maintenance_workflow_changed": "Review the current workflow bytes and exact digest. "
    "The tested head, merge, and current default branch must agree.",
    "maintenance_merge_changed": "Read the current merged PR and use its exact tested head and merge commit in a new preview.",
    "maintenance_principal_authority_revoked": "The original owner needs current platform and plan-approval authority "
    "before this recovery can proceed.",
    "maintenance_provider_unavailable": "GitHub verification could not complete. Retry the read-only preview after provider access is restored.",
}


async def call(*, accept, flow_id, body, current_user, db, access):
    from src.agentauth.bootstrap import BootstrapRefusedError
    from src.agentauth.human_control import authorize_human_session

    from .routes import _resolve_actor_role

    try:
        human = await authorize_human_session(current_user, db)
    except BootstrapRefusedError:
        raise HTTPException(403, "An authenticated human maintenance approver is required.") from None
    actor = ApprovalContext(
        org_id=human.tenant_id, actor_id=human.user_id, actor_role=await _resolve_actor_role(access, current_user), reason=body.reason
    )
    try:
        result = await adopt_maintenance(db, flow_id=flow_id, actor=actor, request=body, accept=accept)
        if accept:
            await db.commit()
        return result
    except (CycleBlockedError, SharedAppendError, ProposalRejectedError) as error:
        await db.rollback()
        code = error.reason if isinstance(error, CycleBlockedError) else str(error)
        raise HTTPException(
            409,
            {
                "code": code,
                "next_action": RECOVERY_ACTIONS.get(
                    code,
                    "Review the current flow, its retained repair history, and the exact maintenance request; "
                    "resolve the reported prerequisite before previewing again.",
                ),
            },
        ) from None
    except httpx.HTTPError:
        await db.rollback()
        code = "maintenance_provider_unavailable"
        raise HTTPException(503, {"code": code, "next_action": RECOVERY_ACTIONS[code]}) from None


@router.post("/flows/{flow_id}/maintenance-adoption/preview")
async def preview(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: MaintenanceAdoptionRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    access = AccessControl(db)
    access.require_platform_admin(current_user)
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    return await call(accept=False, flow_id=flow_id, body=body, current_user=current_user, db=db, access=access)


@router.post("/flows/{flow_id}/maintenance-adoption/accept")
async def accept(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: MaintenanceAdoptionRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    access = AccessControl(db)
    access.require_platform_admin(current_user)
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    return await call(accept=True, flow_id=flow_id, body=body, current_user=current_user, db=db, access=access)
