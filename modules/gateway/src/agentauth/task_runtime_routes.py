"""Transport and TokenReview protected task bootstrap/attempt adapters."""
from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from src.agentauth.adapter import CREDENTIAL_HEADER
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.routes import get_agent_runtime, require_agent_transport
from src.agentauth.run_credential import CredentialError
from src.agentauth.task_routes import task_delivery
from src.agentauth.task_runtime import TaskRuntime
from src.agentauth.workload import WORKLOAD_HEADER, WorkloadRefusedError
from src.tasks.store import TaskStore, TaskStoreError

UUID4 = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
TASK_ID = r"^tsk_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
router = APIRouter(prefix="/internal/v1/agent/task", tags=["task-api"], dependencies=[Depends(require_agent_transport)])


class Workload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pod_uid: str = Field(pattern=UUID4)
    namespace: str = Field(min_length=1, max_length=63)
    pod_name: str | None = Field(default=None, max_length=253)
    job_uid: str | None = Field(default=None, pattern=UUID4)


class BootstrapBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"]
    task_id: str = Field(pattern=TASK_ID)
    invocation_id: str = Field(pattern=UUID4)
    envelope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    workload: Workload


class AttemptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"]
    task_id: str = Field(pattern=TASK_ID)
    invocation_id: str = Field(pattern=UUID4)
    generation: int = Field(ge=1, le=64, strict=True)
    runtime_attempt_id: str = Field(pattern=UUID4)
    protocol_version: Literal[1]
    capabilities: list[Literal["input", "cancel"]] = Field(min_length=1)
    old_attempt_invalidated: Literal[True]


def task_runtime(runtime, *, stop_only=False):
    env = os.environ if runtime.env is None else runtime.env
    if not stop_only and env.get("ADP_RUN_TASKS_ENABLED", "false").lower() != "true":
        raise HTTPException(503, "task runtime unavailable")
    return TaskRuntime(TaskStore(dynamodb_client=runtime.store.client,
        table_name=env.get("WEBHOOK_EVENTS_TABLE"), authority_table_name=runtime.store.table), env=env)


async def _authenticate(request, *, require_attempt, stop_only=False):
    runtime = get_agent_runtime()
    try:
        pod = await run_in_threadpool(runtime.workloads.verify, request.headers.get(WORKLOAD_HEADER, ""))
        identity = await run_in_threadpool(task_runtime(runtime, stop_only=stop_only).authenticate,
            credential=request.headers.get(CREDENTIAL_HEADER, ""), pod=pod, require_attempt=require_attempt, stop_only=stop_only)
        if await run_in_threadpool(runtime.workloads.verify, request.headers.get(WORKLOAD_HEADER, "")) != pod:
            raise WorkloadRefusedError("workload changed")
        return identity
    except (BootstrapRefusedError, CredentialError, WorkloadRefusedError, TaskStoreError):
        raise HTTPException(404, "not found") from None


async def authenticate_task_attempt(request: Request):
    return await _authenticate(request, require_attempt=True)


async def authenticate_task_settlement(request: Request):
    """Only terminal/stop evidence routes may consume this restricted identity."""
    return await _authenticate(request, require_attempt=True, stop_only=True)


@router.post("/bootstrap")
async def bootstrap(body: BootstrapBody, request: Request, runtime=Depends(get_agent_runtime)):
    try:
        pod = await run_in_threadpool(runtime.workloads.verify, request.headers.get(WORKLOAD_HEADER, ""))
        result = await run_in_threadpool(task_runtime(runtime).bootstrap,
            body=body.model_dump(exclude_none=True), pod=pod, delivery=task_delivery(runtime))
        if await run_in_threadpool(runtime.workloads.verify, request.headers.get(WORKLOAD_HEADER, "")) != pod:
            raise WorkloadRefusedError("workload changed")
        return result
    except (BootstrapRefusedError, CredentialError, WorkloadRefusedError, TaskStoreError):
        raise HTTPException(404, "not found") from None


@router.post("/attempt")
async def attempt(body: AttemptBody, request: Request, runtime=Depends(get_agent_runtime)):
    identity = await _authenticate(request, require_attempt=False)
    try:
        await run_in_threadpool(task_runtime(runtime).register_attempt, identity=identity, body=body.model_dump())
    except (BootstrapRefusedError, TaskStoreError):
        raise HTTPException(409, "task attempt refused") from None
    return {"schema_version": "1.0", "operation_status": "confirmed", "request_id": body.runtime_attempt_id}


class TaskRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(pattern=TASK_ID)
    invocation_id: str = Field(pattern=UUID4)
    generation: int = Field(ge=1, le=64, strict=True)


class TaskAttemptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run: TaskRunBody
    runtime_attempt_id: str = Field(pattern=UUID4)


class TurnBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"]
    attempt: TaskAttemptBody
    request_id: str = Field(pattern=UUID4)
    expected_transcript_version: int = Field(ge=1, strict=True)


def require_body_attempt(identity, attempt):
    if (identity.task_id, identity.invocation_id, identity.generation, identity.runtime_attempt_id) != (
            attempt.run.task_id, attempt.run.invocation_id, attempt.run.generation, attempt.runtime_attempt_id):
        raise HTTPException(404, "not found")


@router.post("/turn")
async def turn(body: TurnBody, request: Request, runtime=Depends(get_agent_runtime)):
    from src.agentauth.task_turns import TaskTurnStore

    identity = await authenticate_task_attempt(request)
    require_body_attempt(identity, body.attempt)
    try:
        return await run_in_threadpool(TaskTurnStore(task_runtime(runtime).repository).commit,
            identity=identity, request_id=body.request_id, expected_transcript_version=body.expected_transcript_version)
    except TaskStoreError:
        raise HTTPException(409, "task turn refused") from None
