"""Plan-approver API for explicit continuation of legacy code delivery."""

import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from .compile import ApprovalContext
from .continuation import ContinuationRefusedError, ContinuationRequest, accept_continuation, preview_continuation
from .controls import get_run_binding_resolver
from .pr_identity import resolve_pr_identity

router = APIRouter()
ENABLED_ENV = "ADP_SHARED_WORKER_CONTINUATION_ENABLED"


async def _call(*, accept, flow_id, body, current_user, access, db, resolver):
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
        reason=body.reconciliation_evidence,
    )
    ready = all(os.environ.get(name, "false").strip().lower() == "true" for name in (ENABLED_ENV, "ADP_SHARED_RUN_REPORTING_ENABLED"))
    if accept and not ready:
        raise HTTPException(
            409, {"code": "continuation_not_enabled", "detail": "Deploy and enable the shared-worker continuation adapter before acceptance."}
        )
    try:
        result = await (accept_continuation if accept else preview_continuation)(
            db,
            flow_id=flow_id,
            actor=actor,
            request=body,
            resolver=resolver,
            resolve_pr=resolve_pr_identity,
        )
        if accept:
            await db.commit()
        else:
            result.pop("initial_runs", None)
            result.pop("database_snapshot", None)
            result["adapter_enabled"] = ready
        return result
    except ContinuationRefusedError as error:
        await db.rollback()
        raise HTTPException(404 if error.code == "flow_not_found" else 409, {"code": error.code, "detail": error.detail}) from None
    except ValueError as error:
        await db.rollback()
        raise HTTPException(422, str(error)) from None


# The ordinary operator authentication/RBAC dependencies apply to BOTH requests.
# A preview contains acceptance records and therefore needs PLAN_APPROVE too.
@router.post("/flows/{flow_id}/continuation/preview")
async def preview_existing_flow(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: ContinuationRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    resolver=Depends(get_run_binding_resolver),
):
    access = AccessControl(db)
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    return await _call(accept=False, flow_id=flow_id, body=body, current_user=current_user, access=access, db=db, resolver=resolver)


@router.post("/flows/{flow_id}/continuation/accept")
async def accept_existing_flow(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: ContinuationRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    resolver=Depends(get_run_binding_resolver),
):
    access = AccessControl(db)
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    return await _call(accept=True, flow_id=flow_id, body=body, current_user=current_user, access=access, db=db, resolver=resolver)
