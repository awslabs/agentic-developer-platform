"""Standing human Task enrollment, administered without service impersonation."""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
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
from src.tasks.personas import CODEX_REPORT_PERSONAS
from src.tasks.repository_authority import CODING_PERSONAS, safe_path

router = APIRouter(prefix="/human-principals", tags=["human-task-admin"])


class RepositoryScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    repository_id: int = Field(gt=0)
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    path_prefixes: list[str] = Field(min_length=1, max_length=32)

    @field_validator("repository_id", mode="before")
    @classmethod
    def stored_integer(cls, value):
        return int(value) if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value() else value

    @field_validator("path_prefixes")
    @classmethod
    def valid_paths(cls, value):
        if len(set(value)) != len(value) or any(not safe_path(path.rstrip("/")) for path in value):
            raise ValueError("Use distinct repository paths")
        return value


class HumanTaskPolicyPut(TaskPolicyPutRequest):
    repository_scopes: list[RepositoryScope] = Field(default_factory=list, max_length=16)


class HumanTaskPolicyResponse(TaskPolicyResponse):
    repository_scopes: list[RepositoryScope] = Field(default_factory=list)


async def enrolled_target(db, current_user, user_id):
    await _require_human_org_admin(db, current_user)
    try:
        admin = await authorize_human_session(current_user, db)
        locator = human_locator(user_id)
        await require_live_human_membership(db, user_id=locator.removeprefix("human:"), tenant_id=admin.tenant_id)
    except (ValueError, BootstrapRefusedError):
        raise HTTPException(404, "Human Task principal not found") from None
    return admin, locator


@router.get("/{user_id}/task-policy", response_model=HumanTaskPolicyResponse)
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
    return HumanTaskPolicyResponse.model_validate(policy)


@router.put("/{user_id}/task-policy", response_model=HumanTaskPolicyResponse)
async def put_policy(
    user_id: str,
    body: HumanTaskPolicyPut,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    policy_store: Annotated[TaskServicePolicyStore, Depends(task_policy_store)],
):
    admin, locator = await enrolled_target(db, current_user, user_id)
    # Only installed canonical Task executables qualify. Repository developer
    # authority cannot be created by labelling an investigator as a developer.
    if set(body.allowed_personas) - {
        "agent-task-investigator",
        "agent-task-cyber",
        "agent-task-gpt-developer",
        *CODING_PERSONAS,
        *CODEX_REPORT_PERSONAS,
    }:
        raise HTTPException(422, "Unsupported Task executable")
    if CODING_PERSONAS.intersection(body.allowed_personas) and not getattr(body, "repository_scopes", []):
        raise HTTPException(422, "Coding Tasks require explicit repository scope")
    # Native developer Tasks fetch through the administrator-owned repository
    # binding, rather than the legacy bounded-file snapshot protocol. Admission
    # still freezes this binding, tools, model revision and acceptance checks.
    if "agent-task-gpt-developer" in body.allowed_personas and (
        not body.repositories or any(not binding.validation_checks for binding in body.repositories.values())
    ):
        raise HTTPException(422, "Native developer Tasks require repository bindings with validation checks")
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
    return HumanTaskPolicyResponse.model_validate(policy)
