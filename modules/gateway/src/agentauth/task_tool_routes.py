"""Run-bound authorization for separately deployed generic tool services.

The caller's registered IAM transport is independent of the forwarded worker
identity. Authorization never turns a tool service into the Task's owner.
"""

from __future__ import annotations

import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.agentauth.routes import require_agent_transport
from src.agentauth.task_agent_runtime import get_task_agent_runtime
from src.agentauth.task_runtime_routes import (
    TaskAttemptBody,
    authenticate_task_attempt,
    authenticate_task_settlement,
    require_body_attempt,
    task_runtime,
)
from src.agentauth.task_service_policy import TaskServicePolicyError, TaskServicePolicyStore
from src.agentauth.task_tool_policy import TOOL_PATTERN, TaskToolPolicyError, persona_tools, valid_tools
from src.shared.database import get_db
from src.tasks.human_authority import require_current_owner
from src.tasks.store import WorkBindingError, _protected_grant_digest

router = APIRouter(prefix="/internal/v1/agent/task", tags=["task-api"], dependencies=[Depends(require_agent_transport)])


class ToolAuthorizationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = Field(pattern=r"^1\.0$")
    attempt: TaskAttemptBody
    tool: str = Field(pattern=TOOL_PATTERN)
    cleanup: bool = Field(default=False, strict=True)


def authorize_tool(repo, policies, identity, tool, *, cleanup=False, env=None):
    task = repo.read_task(identity.task_id)
    if (
        not task
        or task.get("scope") != {"tenant": identity.tenant, "canonical_principal": identity.canonical_principal}
        or (task.get("invocation_id"), int(task.get("generation", 0)), task.get("runtime_attempt_id"))
        != (identity.invocation_id, identity.generation, identity.runtime_attempt_id)
    ):
        raise HTTPException(403, "Task tool ownership refused")
    grant = repo._get_authority("TENANT#" + identity.tenant, f"TASK_RUN#{identity.invocation_id}#GEN#{identity.generation:010d}")
    frozen = task.get("tool_grants", [])
    if not grant or not valid_tools(frozen) or frozen != grant.get("tool_grants", []) or _protected_grant_digest(grant) != task.get("grant_digest"):
        raise HTTPException(403, "Task tool grant refused")
    if cleanup:
        # Stop-only exception: even a model-only Task must be able to prove
        # there is no outstanding domain work before terminal settlement.
        # The service must bind every cancelled resource to this verified Task.
        _, operation = tool.split(".")
        if operation != "cancel_jobs":
            raise HTTPException(403, "Only Task cleanup is permitted")
    else:
        if (
            task["state"] not in {"accepted", "queued", "running", "waiting_for_input"}
            or time.time() >= datetime.fromisoformat(task["deadline_at"].replace("Z", "+00:00")).timestamp()
        ):
            raise HTTPException(403, "Task no longer authorizes tools")
        policy = policies.get(tenant_id=identity.tenant, canonical_principal_id=identity.canonical_principal)
        if (
            not policy
            or policy.get("status") != "active"
            or not valid_tools(policy.get("allowed_tools", []))
            or task["persona"] not in policy.get("allowed_personas", [])
        ):
            raise HTTPException(403, "Task tool policy refused")
        if tool not in frozen or tool not in policy.get("allowed_tools", []) or tool not in persona_tools(task["persona"], env):
            raise HTTPException(403, "Task tool permission refused")
    # Return only execution input/identity, never protected credentials/budget grants.
    snapshot = {
        key: task[key]
        for key in (
            "task_id",
            "invocation_id",
            "generation",
            "version",
            "runtime_attempt_id",
            "scope",
            "persona",
            "state",
            "input_payload",
            "deadline_at",
            "tool_grants",
        )
        if key in task
    }
    snapshot["version"] = int(task["version"])
    return {
        "schema_version": "1.0",
        "identity": {
            key: getattr(identity, key) for key in ("task_id", "invocation_id", "generation", "runtime_attempt_id", "tenant", "canonical_principal")
        },
        "task": snapshot,
    }


@router.post("/tool-authorize")
async def tool_authorize(body: ToolAuthorizationBody, request: Request, db: AsyncSession = Depends(get_db)):
    authenticate = authenticate_task_settlement if body.cleanup else authenticate_task_attempt
    identity = await authenticate(request)
    require_body_attempt(identity, body.attempt)
    if not body.cleanup:
        await require_current_owner(db, tenant=identity.tenant, principal=identity.canonical_principal)
    runtime = get_task_agent_runtime()
    repo = task_runtime(runtime, stop_only=body.cleanup).repository
    policies = TaskServicePolicyStore(table_name=repo.authority_table_name, client=repo._client)
    try:
        result = await run_in_threadpool(authorize_tool, repo, policies, identity, body.tool, cleanup=body.cleanup)
        if await authenticate(request) != identity:
            raise HTTPException(403, "Task tool identity changed")
        return result
    except (TaskToolPolicyError, TaskServicePolicyError, WorkBindingError):
        raise HTTPException(403, "Task tool authority unavailable") from None
