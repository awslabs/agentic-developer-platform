"""Standing human Task enrollment, administered without service impersonation."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.admin.persona_models.routes import _require_human_org_admin, task_policy_store
from src.admin.persona_models.schemas import TaskPolicyPutRequest, TaskPolicyResponse
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.human_control import authorize_human_session, require_live_human_membership
from src.agentauth.task_service_policy import TaskServicePolicyError, TaskServicePolicyStore
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext
from src.tasks.human_authority import human_locator

router = APIRouter(prefix="/human-principals", tags=["human-task-admin"])


async def enrolled_target(db, current_user, user_id):
    await _require_human_org_admin(db, current_user)
    try:
        admin = await authorize_human_session(current_user, db)
        locator = human_locator(user_id)
        await require_live_human_membership(db, user_id=locator.removeprefix("human:"), tenant_id=admin.tenant_id)
    except (ValueError, BootstrapRefusedError):
        raise HTTPException(404, "Human Task principal not found") from None
    return admin, locator


@router.get("/{user_id}/task-policy", response_model=TaskPolicyResponse)
async def get_policy(
    user_id: str,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    policy_store: Annotated[TaskServicePolicyStore, Depends(task_policy_store)],
):
    admin, locator = await enrolled_target(db, current_user, user_id)
    try:
        policy = await run_in_threadpool(policy_store.get, tenant_id=admin.tenant_id, canonical_principal_id=locator)
    except TaskServicePolicyError:
        raise HTTPException(503, "Task policy unavailable") from None
    if policy is None:
        raise HTTPException(404, "Task policy not found")
    return TaskPolicyResponse.model_validate(policy)


@router.put("/{user_id}/task-policy", response_model=TaskPolicyResponse)
async def put_policy(
    user_id: str,
    body: TaskPolicyPutRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    policy_store: Annotated[TaskServicePolicyStore, Depends(task_policy_store)],
):
    admin, locator = await enrolled_target(db, current_user, user_id)
    # Only installed canonical Task executables qualify. Repository developer
    # authority cannot be created by labelling an investigator as a developer.
    if set(body.allowed_personas) - {"agent-task-investigator", "agent-task-cyber"}:
        raise HTTPException(422, "Unsupported Task executable")
    try:
        policy = await run_in_threadpool(
            policy_store.put,
            tenant_id=admin.tenant_id,
            canonical_principal_id=locator,
            expected_version=body.expected_version,
            policy=body.model_dump(exclude={"expected_version"}, mode="python"),
            updated_by=admin.user_id,
        )
    except TaskServicePolicyError as exc:
        raise HTTPException({"version_conflict": 409, "invalid_policy": 422}.get(exc.code, 503), "Task policy update refused") from None
    return TaskPolicyResponse.model_validate(policy)
