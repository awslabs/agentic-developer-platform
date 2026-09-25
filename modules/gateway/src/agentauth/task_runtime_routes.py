"""Transport and TokenReview protected task bootstrap/attempt adapters."""

from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool

from src.agentauth.adapter import CREDENTIAL_HEADER
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.routes import require_agent_transport
from src.agentauth.run_credential import CredentialError
from src.agentauth.task_agent_runtime import get_task_agent_runtime as get_agent_runtime
from src.agentauth.task_routes import task_delivery
from src.agentauth.task_runtime import TaskRuntime
from src.agentauth.workload import WORKLOAD_HEADER, WorkloadRefusedError
from src.shared.database import get_db
from src.tasks.store import StaleAttemptError, StaleGenerationError, TaskStore, TaskStoreError, WorkBindingError

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
    if not stop_only and env.get("ADP_TASK_API_WORKER_ENABLED", "false").lower() != "true":
        raise HTTPException(503, "task runtime unavailable")
    return TaskRuntime(
        TaskStore(dynamodb_client=runtime.store.client, table_name=env.get("WEBHOOK_EVENTS_TABLE"), authority_table_name=runtime.store.table), env=env
    )


async def _authenticate(request, *, require_attempt, stop_only=False):
    runtime = get_agent_runtime()
    try:
        pod = await run_in_threadpool(runtime.workloads.verify, request.headers.get(WORKLOAD_HEADER, ""))
        identity = await run_in_threadpool(
            task_runtime(runtime, stop_only=stop_only).authenticate,
            credential=request.headers.get(CREDENTIAL_HEADER, ""),
            pod=pod,
            require_attempt=require_attempt,
            stop_only=stop_only,
        )
        if await run_in_threadpool(runtime.workloads.verify, request.headers.get(WORKLOAD_HEADER, "")) != pod:
            raise WorkloadRefusedError("workload changed")
        return identity
    except (BootstrapRefusedError, CredentialError, WorkloadRefusedError, TaskStoreError, WorkBindingError):
        raise HTTPException(404, "not found") from None


async def authenticate_task_attempt(request: Request):
    return await _authenticate(request, require_attempt=True)


async def authenticate_task_settlement(request: Request):
    """Only terminal/stop evidence routes may consume this restricted identity."""
    runtime = get_agent_runtime()
    try:
        token = request.headers.get(WORKLOAD_HEADER, "")
        pod = await run_in_threadpool(runtime.workloads.verify, token)
        identity = await run_in_threadpool(task_runtime(runtime, stop_only=True).authenticate_settlement, pod=pod)
        if await run_in_threadpool(runtime.workloads.verify, token) != pod:
            raise WorkloadRefusedError("workload changed")
        return identity
    except (BootstrapRefusedError, WorkloadRefusedError, TaskStoreError, WorkBindingError):
        from src.tasks import errors

        raise errors.not_found() from None


@router.post("/bootstrap")
async def bootstrap(body: BootstrapBody, request: Request, runtime=Depends(get_agent_runtime)):
    try:
        pod = await run_in_threadpool(runtime.workloads.verify, request.headers.get(WORKLOAD_HEADER, ""))
        result = await run_in_threadpool(
            task_runtime(runtime).bootstrap, body=body.model_dump(exclude_none=True), pod=pod, delivery=task_delivery(runtime)
        )
        if await run_in_threadpool(runtime.workloads.verify, request.headers.get(WORKLOAD_HEADER, "")) != pod:
            raise WorkloadRefusedError("workload changed")
        return result
    except (BootstrapRefusedError, CredentialError, WorkloadRefusedError, TaskStoreError, WorkBindingError):
        raise HTTPException(404, "not found") from None


@router.post("/attempt")
async def attempt(body: AttemptBody, request: Request, runtime=Depends(get_agent_runtime)):
    from src.agentauth.exit_retention import ExitRetentionError

    identity = await _authenticate(request, require_attempt=False)
    try:
        await run_in_threadpool(task_runtime(runtime).register_attempt, identity=identity, body=body.model_dump())
        pod = await run_in_threadpool(runtime.workloads.verify, request.headers.get(WORKLOAD_HEADER, ""))
        if pod.uid != identity.pod_uid:
            raise WorkloadRefusedError("task workload changed")
        await run_in_threadpool(
            runtime.workloads.exit_retention.retain,
            name=pod.name,
            uid=pod.uid,
            invocation_id=identity.invocation_id,
            tenant_id=identity.tenant,
        )
    except (ExitRetentionError, WorkloadRefusedError):
        raise HTTPException(503, "task exit evidence retention unavailable") from None
    except (BootstrapRefusedError, TaskStoreError, WorkBindingError, StaleAttemptError, StaleGenerationError):
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
    allow_autonomous: bool = Field(default=False, strict=True)


def require_body_attempt(identity, attempt):
    if (identity.task_id, identity.invocation_id, identity.generation, identity.runtime_attempt_id) != (
        attempt.run.task_id,
        attempt.run.invocation_id,
        attempt.run.generation,
        attempt.runtime_attempt_id,
    ):
        raise HTTPException(404, "not found")


@router.post("/turn")
async def turn(body: TurnBody, request: Request, runtime=Depends(get_agent_runtime)):
    from src.agentauth.task_turns import TaskTurnStore

    identity = await authenticate_task_attempt(request)
    require_body_attempt(identity, body.attempt)
    try:
        return await run_in_threadpool(
            TaskTurnStore(task_runtime(runtime).repository).commit,
            identity=identity,
            request_id=body.request_id,
            expected_transcript_version=body.expected_transcript_version,
            allow_autonomous=body.allow_autonomous,
        )
    except (TaskStoreError, WorkBindingError):
        raise HTTPException(409, "task turn refused") from None


class ModelText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=32000)


class ModelMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: list[ModelText] = Field(min_length=1, max_length=16)


class ModelToolUse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["tool_use"]
    id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    input: dict


class ModelToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["tool_result"]
    tool_use_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    content: str | list[ModelText] = Field(max_length=32000)
    is_error: bool | None = Field(default=None, strict=True)


class SdkMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str | list[ModelText | ModelToolUse | ModelToolResult] = Field(min_length=1, max_length=32000)

    @model_validator(mode="after")
    def role_blocks(self):
        if isinstance(self.content, list):
            if len(self.content) > 64:
                raise ValueError("too many content blocks")
            for block in self.content:
                if isinstance(block, ModelToolUse) and self.role != "assistant":
                    raise ValueError("tool_use requires assistant role")
                if isinstance(block, ModelToolResult) and self.role != "user":
                    raise ValueError("tool_result requires user role")
        return self


class SdkTool(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    description: str | None = Field(default=None, max_length=16000)
    input_schema: dict

    @model_validator(mode="after")
    def object_schema(self):
        if self.input_schema.get("type") != "object":
            raise ValueError("custom tool requires object input schema")
        return self


class SdkToolChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["auto", "any", "tool", "none"]
    name: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    disable_parallel_tool_use: bool | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def named_tool(self):
        if (self.type == "tool") != (self.name is not None):
            raise ValueError("only a named tool choice accepts name")
        return self


class SdkRequest(BaseModel):
    """Custom tools only; no server tools, URLs, media, credentials or model override."""

    model_config = ConfigDict(extra="forbid")
    messages: list[SdkMessage] = Field(min_length=1, max_length=32)
    system: str | list[ModelText] | None = Field(default=None, max_length=16000)
    tools: list[SdkTool] | None = Field(default=None, max_length=32)
    tool_choice: SdkToolChoice | None = None
    stop_sequences: list[str] | None = Field(default=None, max_length=16)

    @model_validator(mode="after")
    def bounded_custom_tools(self):
        names = [tool.name for tool in self.tools or []]
        if len(names) != len(set(names)):
            raise ValueError("duplicate tool name")
        if self.tool_choice and self.tool_choice.type == "tool" and self.tool_choice.name not in names:
            raise ValueError("tool choice must name a declared tool")
        if any(not value or len(value) > 1000 for value in self.stop_sequences or []):
            raise ValueError("invalid stop sequence")
        return self


class ModelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"]
    attempt: TaskAttemptBody
    turn_id: str = Field(pattern=UUID4)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    messages: list[ModelMessage] | None = Field(default=None, min_length=1, max_length=32)
    max_tokens: int = Field(ge=1, le=4096, strict=True)
    system: str | None = Field(default=None, max_length=16000)
    sdk_request: SdkRequest | None = None

    @model_validator(mode="after")
    def exclusive_request(self):
        if (self.messages is None) == (self.sdk_request is None):
            raise ValueError("supply exactly one model request form")
        if self.sdk_request is not None and self.system is not None:
            raise ValueError("SDK system belongs inside sdk_request")
        return self

    def invocation(self):
        if self.sdk_request is not None:
            return {**self.sdk_request.model_dump(exclude_none=True), "max_tokens": self.max_tokens}
        return self.model_dump(include={"messages", "max_tokens", "system"}, exclude_none=True)


@router.post("/model")
async def model(body: ModelBody, request: Request, runtime=Depends(get_agent_runtime), db=Depends(get_db)):
    from src.agentauth.model_policy import ModelPolicyError
    from src.agentauth.task_budget import TaskBudgetError
    from src.agentauth.task_model import TaskModel

    identity = await authenticate_task_attempt(request)
    require_body_attempt(identity, body.attempt)
    invocation = body.invocation()
    import json

    if len(json.dumps(invocation, ensure_ascii=False).encode()) > 65536:
        raise HTTPException(413, "task model request too large")
    try:
        return await TaskModel(task_runtime(runtime).repository, db=db).execute(
            identity=identity, turn_id=body.turn_id, request_digest=body.request_digest, request=invocation, sdk_request=body.sdk_request is not None
        )
    except (TaskStoreError, WorkBindingError, ModelPolicyError, TaskBudgetError):
        raise HTTPException(409, "task model refused") from None
