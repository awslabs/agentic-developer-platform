"""Human-only collection of bounded L5 UI facts; no release acceptance endpoint."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.human_control import authorize_human_session
from src.auth.dependencies import get_current_user
from src.auth.native_launch_revocation import require_native_launch_session
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from .l5_observation_contract import EndorseObservation, PrepareObservation, RecordObservation
from .l5_observation_scope import ObservationError
from .l5_observations import endorse, prepare, read, record
from .review_cycle import CycleBlockedError

router = APIRouter()
NEXT_ACTION = {
    "l5_observation_plan_changed": "Read the current accepted plan and attempt, then prepare for that scope; do not repeat native work.",
    "l5_observation_check_not_accepted": "Select a check from this release's original accepted delivery contract.",
    "l5_observation_claim_changed": "Wait for the current execution owner to settle; the selected worker no longer holds this release claim.",
    "l5_observation_execution_changed": "Read the current execution; collection requires an active, unpaused flow and running release assignment.",
    "l5_observation_assignment_changed": "Select the current assignment and its confirmed dispatch before preparing another observation.",
    "l5_observation_workflow_unverified": "Wait for the confirmed dispatch's workflow job to authenticate with the gateway, then retry preparation.",
    "l5_observation_workflow_changed": "Review the dispatch and exact run identity; do not substitute another run or repeat native work.",
    "l5_observation_workflow_not_running": "The exact workflow must be in progress; a completed run needs historical evidence review.",
    "l5_observation_workflow_provider_unavailable": "Restore GitHub App read access and retry verification; do not redispatch the workflow.",
    "l5_observation_source_changed": "Review the authorized release revision before preparing another observation; do not repeat native work.",
    "l5_observation_runtime_unavailable": "Repair the installed L5 wheel and schema identity through the deployment procedure, then retry this read.",
    "l5_observation_expired": "Prepare a fresh bounded observation with a current human session and release scope.",
    "l5_observation_request_key_reused": "Use a new request_key for a different observation; an existing key can only replay the same preparation.",
    "l5_observation_owner_endorsement_required": "The selected Product owner must endorse this exact snapshot before observation or launch.",
    "l5_observation_already_recorded": "Read the existing immutable observation. A different observation needs a new challenge.",
    "l5_observation_launch_already_attempted": "Read this challenge's launch_attempt receipts. Do not repeat an issued or uncertain federation.",
    "l5_observation_owner_revoked": "The original owner's current workspace authority is missing; restore authorized membership before continuing.",
    "l5_observation_observer_revoked": "Select a currently authorized human observer before preparing another observation.",
}


async def human_session(request, current_user, db):
    if current_user.token_use != "access" or current_user.expires_at is None:
        raise HTTPException(401, detail={"code": "session_required"})
    try:
        human = await authorize_human_session(current_user, db)
    except BootstrapRefusedError:
        raise HTTPException(403, detail={"code": "l5_observation_human_required"}) from None
    await require_native_launch_session(request.headers.get("Authorization", ""))
    from src.auth.security_fixture_credential import require_interactive_credential

    await require_interactive_credential(request, current_user, db)
    return human


async def invoke(operation, db, response, **kwargs):
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await operation(db, **kwargs)
        if operation is not read:
            await db.commit()
        return result
    except (ObservationError, CycleBlockedError) as error:
        await db.rollback()
        code = error.code if isinstance(error, ObservationError) else error.reason
        raise HTTPException(
            error.status if isinstance(error, ObservationError) else 409,
            detail={
                "code": code,
                "next_action": NEXT_ACTION.get(
                    code,
                    "Read the current observation and selected release scope. Resolve changed or missing evidence "
                    "before preparing a new observation; do not repeat native work.",
                ),
            },
        ) from None


@router.post("/flows/{flow_id}/l5-observations/prepare")
async def prepare_observation(
    flow_id: str,
    body: PrepareObservation,
    request: Request,
    response: Response,
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    human = await human_session(request, current_user, db)
    access = AccessControl(db)
    access.require_platform_admin(current_user)
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    return await invoke(prepare, db, response, flow_id=flow_id, human=human, request=body)


@router.get("/flows/{flow_id}/l5-observations/{challenge_id}")
async def read_observation(
    flow_id: str,
    challenge_id: str,
    request: Request,
    response: Response,
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    human = await human_session(request, current_user, db)
    await AccessControl(db).check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    return await invoke(read, db, response, flow_id=flow_id, challenge_id=challenge_id, human=human)


@router.post("/flows/{flow_id}/l5-observations/{challenge_id}/observe")
async def record_observation(
    flow_id: str,
    challenge_id: str,
    body: RecordObservation,
    request: Request,
    response: Response,
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    human = await human_session(request, current_user, db)
    await AccessControl(db).check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    return await invoke(record, db, response, flow_id=flow_id, challenge_id=challenge_id, human=human, body=body)


@router.post("/flows/{flow_id}/l5-observations/{challenge_id}/endorse")
async def endorse_observation(
    flow_id: str,
    challenge_id: str,
    body: EndorseObservation,
    request: Request,
    response: Response,
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    human = await human_session(request, current_user, db)
    await AccessControl(db).check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    return await invoke(endorse, db, response, flow_id=flow_id, challenge_id=challenge_id, human=human, body=body)


@router.get("/flows/{flow_id}/l5-observations/{challenge_id}/effects")
async def read_request_effect(
    flow_id: str,
    challenge_id: str,
    request: Request,
    response: Response,
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from .l5_effect_read import read_effect

    human = await human_session(request, current_user, db)
    await AccessControl(db).check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    return await invoke(read_effect, db, response, flow_id=flow_id, challenge_id=challenge_id, human=human)
