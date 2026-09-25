"""Task API command and stop-only adapters. Body fields never grant authority."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.agentauth.routes import require_agent_transport
from src.shared.database import get_db
from src.tasks import authz, errors, http
from src.tasks.routes import get_store
from src.tasks.task_commands import TaskCommands

UUID = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")]
TASK = Annotated[str, Field(pattern=r"^tsk_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")]
ARTIFACT = Annotated[str, Field(pattern=r"^art_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")]
CURSOR = Annotated[str, Field(pattern=r"^tsk_[0-9a-f-]+:[1-9][0-9]*$")]


def valid_timestamp(value: str) -> str:
    datetime.fromisoformat(value)
    return value


Timestamp = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"), AfterValidator(valid_timestamp)]
Text = Annotated[str, Field(min_length=1, max_length=1000)]


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def closed_scalar_types(cls, values):
        if isinstance(values, dict):
            nullable = {"result", "error", "signal", "exit_code", "last_receipt_cursor", "total_usd"}
            for key, value in values.items():
                if value is None and key not in nullable:
                    raise ValueError(f"{key} cannot be explicitly null")
                if key in {"confirmed", "process_exit_validated"} and type(value) is not bool:
                    raise ValueError(f"{key} must be boolean")
        return values


class Message(Closed):
    schema_version: Literal["1.0"]
    command_id: UUID
    text: str = Field(min_length=1, max_length=4000)
    reply_to: UUID | None = None


class Cancel(Closed):
    schema_version: Literal["1.0"]
    command_id: UUID
    reason: str = Field(default="", max_length=1000)


class Run(Closed):
    task_id: TASK
    invocation_id: UUID
    generation: int = Field(ge=1)


class Attempt(Closed):
    run: Run
    runtime_attempt_id: UUID


class Control(Closed):
    schema_version: Literal["1.0"]
    attempt: Attempt
    last_receipt_cursor: CURSOR | None


class ChildExit(Closed):
    confirmed: Literal[True]
    exit_code: int | None
    signal: str | None
    stopped_at: Timestamp


class Finding(Closed):
    statement: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(min_length=1)
    confidence: Literal["low", "medium", "high"] | None = None


class Evidence(Closed):
    ref: str = Field(min_length=1, max_length=256)
    source: Literal["inputs", "artifact", "instructions", "follow_up_input"]
    artifact_id: ARTIFACT | None = None


class Report(Closed):
    summary: str = Field(min_length=1, max_length=4000)
    findings: list[Finding]
    uncertainties: list[Text]
    recommendations: list[Text]
    evidence_refs: list[Evidence]


class Result(Closed):
    schema_version: Literal["1.0"]
    outcome: Literal["completed"]
    report: Report
    committed_at: Timestamp
    process_exit_validated: Literal[True]
    artifact_ids: list[ARTIFACT] = Field(default_factory=list, max_length=8)
    turns_used: int | None = Field(default=None, ge=1, le=8)
    total_usd: float | None = Field(default=None, ge=0, le=1)


class Failure(Closed):
    schema_version: Literal["1.0"]
    outcome: Literal["failed", "cancelled"]
    code: Literal[
        "admission_recovery_exhausted",
        "deadline_exceeded",
        "model_outcome_unknown",
        "model_access_denied",
        "budget_exceeded",
        "authority_revoked",
        "invalid_agent_output",
        "protocol_violation",
        "process_failed",
        "recovery_exhausted",
        "cancelled_by_client",
        "cancellation_stop_unconfirmed",
        "event_budget_exhausted",
        "storage_unavailable",
    ]
    message: Text
    committed_at: Timestamp
    child_exit_confirmed: bool | None = None
    provider_outcome: Literal["not_started", "prepared", "sent", "confirmed", "unknown"] | None = None
    recovery_required: bool | None = None
    generations_used: int | None = Field(default=None, ge=1, le=3)
    total_usd: float | None = Field(default=None, ge=0)


class Finalize(Closed):
    schema_version: Literal["1.0"]
    attempt: Attempt
    final_report_id: UUID
    child_exit: ChildExit
    outcome: Literal["completed", "failed", "cancelled"]
    result: Result | None
    error: Failure | None
    committed_result_refs: list[ARTIFACT] = Field(max_length=8)

    @model_validator(mode="after")
    def consistency(self):
        if self.outcome == "completed":
            if self.result is None or self.error is not None:
                raise ValueError("completed outcome requires result only")
        elif self.result is not None or self.error is None or self.error.outcome != self.outcome:
            raise ValueError("noncompletion requires matching error only")
        if self.error:
            if self.outcome == "cancelled" and (self.error.child_exit_confirmed is not True or self.error.recovery_required is not False):
                raise ValueError("cancelled finalization requires confirmed exit")
            if self.error.code == "model_outcome_unknown" and (
                self.error.total_usd is not None or self.error.provider_outcome not in {None, "unknown"}
            ):
                raise ValueError("unknown model cost must remain unknown")
        return self


class Workload(Closed):
    pod_uid: UUID
    namespace: str = Field(min_length=1, max_length=63)
    pod_name: str | None = Field(default=None, max_length=253)
    job_uid: UUID | None = None


class Assignment(Closed):
    grant_pk: str
    grant_sk: str
    generation: int = Field(ge=1)


class StopEvidence(Closed):
    child_exit_confirmed: bool
    workload_terminated: bool
    observed_at: Timestamp


class Settlement(Closed):
    schema_version: Literal["1.0"]
    workload: Workload
    assignment: Assignment
    stop_evidence: StopEvidence
    queue_ack_status: Literal["pending", "confirmed", "unknown"]


async def parse(request: Request, model: type[Closed], limit: int = 16384):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise errors.payload_too_large("Task request exceeds its fixed byte limit.")
    try:
        return model.model_validate(http.parse_json_object(bytes(raw)))
    except (ValueError, ValidationError):
        raise errors.invalid_request("Task request does not match the command contract.") from None


router = APIRouter(tags=["task-api"])


async def public_command(request: Request, task_id: str, kind: str, db: AsyncSession):
    http.require_flag("ADP_TASK_API_READ_ENABLED")
    context, scopes = authz.authenticate(request)
    caller = await authz.resolve_caller(context, scopes, db)
    caller.require("adp-tasks/input" if kind == "input" else "adp-tasks/cancel")
    store = get_store()
    await run_in_threadpool(authz.authorize_task, caller, store, task_id)
    body = await parse(request, Message if kind == "input" else Cancel)
    payload = body.model_dump(exclude={"schema_version", "command_id"}, exclude_none=True)
    result = await run_in_threadpool(
        TaskCommands(store.repository).admit,
        task_id=task_id,
        command_id=body.command_id,
        kind=kind,
        payload=payload,
        principal=caller.principal_id,
        tenant=caller.tenant_id,
        expires_at=context.expires_at,
    )
    if kind == "cancel":
        from types import SimpleNamespace

        cancelled = await run_in_threadpool(TaskCommands(store.repository).cancel_unstarted, task_id)
        if cancelled:
            task = await run_in_threadpool(store.repository.read_task, task_id)
            await settle_admission_headroom(
                store.repository,
                SimpleNamespace(task_id=task_id, invocation_id=task["invocation_id"], generation=int(task["generation"]), runtime_attempt_id=None),
            )
    return http.ok(result, status=202)


@router.post("/v1/tasks/{task_id}/messages")
@http.contract_errors
async def message(task_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    return await public_command(request, task_id, "input", db)


@router.post("/v1/tasks/{task_id}/cancel")
@http.contract_errors
async def cancel(task_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    return await public_command(request, task_id, "cancel", db)


def bind(body: Attempt, identity):
    if {**body.run.model_dump(), "runtime_attempt_id": body.runtime_attempt_id} != {
        key: getattr(identity, key) for key in ("task_id", "invocation_id", "generation", "runtime_attempt_id")
    }:
        raise errors.not_found()


@router.post("/internal/v1/agent/task/control", dependencies=[Depends(require_agent_transport)])
@http.contract_errors
async def control(request: Request):
    # Reading cancellation state must survive a short-lived run token expiring.
    # This identity is pod/assignment/current-attempt bound and grants no starts.
    from src.agentauth.task_runtime_routes import authenticate_task_settlement

    identity = await authenticate_task_settlement(request)
    body = await parse(request, Control)
    bind(body.attempt, identity)
    return http.ok(await run_in_threadpool(TaskCommands(get_store().repository).control, identity), status=200)


@router.post("/internal/v1/agent/task/finalize", dependencies=[Depends(require_agent_transport)])
@http.contract_errors
async def finalize(request: Request):
    from src.agentauth.task_runtime_routes import authenticate_task_attempt

    identity = await authenticate_task_attempt(request)
    body = await parse(request, Finalize, 65536)
    bind(body.attempt, identity)
    repository = get_store().repository
    result = await run_in_threadpool(TaskCommands(repository).finalize, identity, body.model_dump(exclude_unset=True))
    await settle_admission_headroom(repository, identity)
    return http.ok(result, status=200)


@router.post("/internal/v1/agent/task/settlement", dependencies=[Depends(require_agent_transport)])
@http.contract_errors
async def settlement(request: Request):
    from src.agentauth.task_runtime_routes import authenticate_task_settlement
    from src.tasks.records import task_authority_partition, task_run_grant_sort_key

    identity = await authenticate_task_settlement(request)
    body = await parse(request, Settlement)
    if body.workload.pod_uid != identity.pod_uid or body.assignment.model_dump() != {
        "grant_pk": task_authority_partition(identity.tenant),
        "grant_sk": task_run_grant_sort_key(invocation_id=identity.invocation_id, generation=identity.generation),
        "generation": identity.generation,
    }:
        raise errors.not_found()
    repository = get_store().repository
    result = await run_in_threadpool(TaskCommands(repository).settlement, identity, body.model_dump(exclude_unset=True))
    await settle_admission_headroom(repository, identity)
    return http.ok(result, status=200)


async def settle_admission_headroom(repository, identity):
    import logging

    from src.agentauth.task_budget_settlement import settle_task_admission

    try:
        await settle_task_admission(repository, identity)
    except Exception as exc:
        # Preserve committed terminal evidence and the conservative hold. The
        # host's settlement retry can reconcile; this never retries inference.
        logging.getLogger(__name__).warning(
            "Task admission settlement remains unconfirmed", extra={"task_id": identity.task_id, "exception_type": type(exc).__name__}
        )
