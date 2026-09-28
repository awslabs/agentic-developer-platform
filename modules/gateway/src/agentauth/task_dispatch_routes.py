"""Producer-proof-protected task dispatch and recovery adapters."""

from __future__ import annotations

import logging
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.agentauth.routes import AgentRuntime
from src.agentauth.task_agent_runtime import get_task_agent_runtime as get_agent_runtime
from src.agentauth.task_work import MAX_WORK_RECORDS_PER_INVOCATION, TaskWorkError, TaskWorkStore, TaskWorkUnavailableError
from src.agentauth.work_routes import verify_producer

logger = logging.getLogger(__name__)
SCHEMA_VERSION = "1.0"
ADMISSION_FLAG = "ADP_TASK_API_ADMISSION_ENABLED"
RECOVERY_FLAG = "ADP_TASK_API_RECOVERY_ENABLED"
DISPATCH_ROLES_ENV = "ADP_TASK_DISPATCH_PRODUCER_ROLES"
RECOVERY_ROLES_ENV = "ADP_TASK_RECOVERY_PRODUCER_ROLES"
UUID4 = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
TIMESTAMP = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$"


def _flag(env, name: str) -> bool:
    return str(env.get(name, "")).strip().lower() == "true"


def _roles(env, name: str) -> set[str]:
    return {role.strip() for role in env.get(name, "").split(",") if role.strip()}


class DispatchClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = Field(pattern=r"^1\.0$")
    dispatch_id: str = Field(pattern=UUID4)
    producer_proof: str = Field(min_length=1, max_length=12000)


class DispatchSettleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = Field(pattern=r"^1\.0$")
    dispatch_id: str = Field(pattern=UUID4)
    lease_token: str = Field(min_length=1, max_length=256)
    publication_outcome: str = Field(pattern=r"^(confirmed|unknown|failed)$")
    sqs_message_id: str | None = Field(max_length=256)
    producer_proof: str = Field(min_length=1, max_length=12000)


class RecoveryClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = Field(pattern=r"^1\.0$")
    shard: str = Field(pattern=r"^v1#(0[0-9]|1[0-5])$")
    cursor: str | None = Field(max_length=4096)
    limit: int = Field(ge=1, le=MAX_WORK_RECORDS_PER_INVOCATION)
    producer_proof: str = Field(min_length=1, max_length=12000)


class RecoveryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern=r"^(publication|workload_termination|queue_ack|retention)$")
    observed: bool
    observed_at: str = Field(pattern=TIMESTAMP)


class RecoverySettleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = Field(pattern=r"^1\.0$")
    work_id: str = Field(pattern=UUID4)
    lease_token: str = Field(min_length=1, max_length=256)
    evidence: RecoveryEvidence
    producer_proof: str = Field(min_length=1, max_length=12000)


def require_adapters_enabled(runtime: AgentRuntime = Depends(get_agent_runtime)) -> None:
    env = os.environ if runtime.env is None else runtime.env
    if not (_flag(env, ADMISSION_FLAG) or _flag(env, RECOVERY_FLAG)):
        raise HTTPException(503, "task dispatch unavailable")


def work_store(runtime: AgentRuntime = Depends(get_agent_runtime)) -> TaskWorkStore:
    env = os.environ if runtime.env is None else runtime.env
    return TaskWorkStore(
        dynamodb_client=runtime.store.client,
        table_name=env.get("WEBHOOK_EVENTS_TABLE") or None,
        authority_table_name=env.get("AGENT_AUTHORITY_TABLE") or runtime.store.table,
    )


router = APIRouter(
    prefix="/internal/v1/tasks",
    tags=["task-dispatch"],
    dependencies=[Depends(require_adapters_enabled)],
)


def _refusal(exc: TaskWorkError) -> HTTPException:
    if exc.code == "not_found":
        return HTTPException(404, "not found")
    if exc.code == "throttled":
        return HTTPException(429, "publication try budget spent")
    if exc.code == "exhausted":
        return HTTPException(409, "recovery exhausted")
    return HTTPException(409, "task work refused")


async def _authenticate(*, proof: str, identity: str, roles_env: str, runtime: AgentRuntime) -> str:
    env = os.environ if runtime.env is None else runtime.env
    allowed = _roles(env, roles_env)
    if not allowed:
        raise HTTPException(503, "task adapter unavailable")
    return await verify_producer(proof, identity, allowed_roles=allowed)


@router.post("/dispatch/claim")
async def dispatch_claim(
    body: DispatchClaimRequest,
    runtime: AgentRuntime = Depends(get_agent_runtime),
    store: TaskWorkStore = Depends(work_store),
) -> JSONResponse:
    await _authenticate(
        proof=body.producer_proof,
        identity=body.dispatch_id,
        roles_env=DISPATCH_ROLES_ENV,
        runtime=runtime,
    )
    try:
        claimed = await run_in_threadpool(store.claim_publication, body.dispatch_id)
    except TaskWorkError as exc:
        logger.info("task dispatch claim refused dispatch=%s reason=%s", body.dispatch_id, exc.code)
        raise _refusal(exc) from None
    except TaskWorkUnavailableError:
        raise HTTPException(503, "task dispatch unavailable") from None
    return JSONResponse(
        {
            "schema_version": SCHEMA_VERSION,
            "envelope": claimed.envelope,
            "lease_token": claimed.work["publication_lease_token"]["S"],
            "lease_expires_at": claimed.work["publication_lease_expires_at"]["S"],
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/dispatch/settle")
async def dispatch_settle(
    body: DispatchSettleRequest,
    runtime: AgentRuntime = Depends(get_agent_runtime),
    store: TaskWorkStore = Depends(work_store),
) -> JSONResponse:
    await _authenticate(
        proof=body.producer_proof,
        identity=body.dispatch_id,
        roles_env=DISPATCH_ROLES_ENV,
        runtime=runtime,
    )
    try:
        settled = await run_in_threadpool(
            store.settle_publication,
            dispatch_id=body.dispatch_id,
            lease_token=body.lease_token,
            publication_outcome=body.publication_outcome,
            sqs_message_id=body.sqs_message_id,
        )
        task_status = await run_in_threadpool(store.task_status, settled.task_id)
    except TaskWorkError as exc:
        logger.info("task dispatch settle refused dispatch=%s reason=%s", body.dispatch_id, exc.code)
        raise _refusal(exc) from None
    except TaskWorkUnavailableError:
        raise HTTPException(503, "task dispatch unavailable") from None
    return JSONResponse(
        {
            "schema_version": SCHEMA_VERSION,
            "dispatch_id": body.dispatch_id,
            "queue_ack_status": settled.work.get("queue_ack_status", {}).get("S", "pending"),
            "task_status": task_status,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/recovery/claim")
async def recovery_claim(
    body: RecoveryClaimRequest,
    runtime: AgentRuntime = Depends(get_agent_runtime),
    store: TaskWorkStore = Depends(work_store),
) -> JSONResponse:
    env = os.environ if runtime.env is None else runtime.env
    if not _flag(env, RECOVERY_FLAG):
        raise HTTPException(503, "task recovery unavailable")
    await _authenticate(
        proof=body.producer_proof,
        identity=body.shard,
        roles_env=RECOVERY_ROLES_ENV,
        runtime=runtime,
    )
    try:
        if not body.cursor:
            from src.agentauth.task_budget import task_budget

            await task_budget(store.repository).reap_abandoned(shard=body.shard)
        claimed, next_cursor = await run_in_threadpool(
            store.claim_recovery,
            shard=body.shard,
            cursor=body.cursor,
            limit=body.limit,
        )
    except TaskWorkError as exc:
        raise _refusal(exc) from None
    except TaskWorkUnavailableError:
        raise HTTPException(503, "task recovery unavailable") from None
    return JSONResponse(
        {
            "schema_version": SCHEMA_VERSION,
            "work": [
                {
                    "work_id": item.work_id,
                    "task_id": item.task_id,
                    "kind": item.kind,
                    "due_at": item.work["due_at"]["S"],
                    "lease_token": item.work["recovery_lease_token"]["S"],
                    "lease_expires_at": item.work["recovery_lease_expires_at"]["S"],
                }
                for item in claimed
            ],
            "next_cursor": next_cursor,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/recovery/settle")
async def recovery_settle(
    body: RecoverySettleRequest,
    runtime: AgentRuntime = Depends(get_agent_runtime),
    store: TaskWorkStore = Depends(work_store),
) -> JSONResponse:
    env = os.environ if runtime.env is None else runtime.env
    if not _flag(env, RECOVERY_FLAG):
        raise HTTPException(503, "task recovery unavailable")
    await _authenticate(
        proof=body.producer_proof,
        identity=body.work_id,
        roles_env=RECOVERY_ROLES_ENV,
        runtime=runtime,
    )
    try:
        if body.evidence.kind == "workload_termination":
            from src.agentauth.task_execution_recovery import recover_execution

            operation_status, task_status = await recover_execution(store.repository, runtime, work_id=body.work_id, lease_token=body.lease_token)
            return JSONResponse(
                {"schema_version": SCHEMA_VERSION, "work_id": body.work_id, "operation_status": operation_status, "task_status": task_status}
            )
        operation_status, settled = await run_in_threadpool(
            store.settle_recovery,
            work_id=body.work_id,
            lease_token=body.lease_token,
            evidence_kind=body.evidence.kind,
            observed=body.evidence.observed,
            observed_at=datetime.fromisoformat(body.evidence.observed_at.replace("Z", "+00:00")),
        )
        task_status = await run_in_threadpool(store.task_status, settled.task_id)
    except TaskWorkError as exc:
        raise _refusal(exc) from None
    except TaskWorkUnavailableError:
        raise HTTPException(503, "task recovery unavailable") from None
    return JSONResponse(
        {
            "schema_version": SCHEMA_VERSION,
            "work_id": body.work_id,
            "operation_status": operation_status,
            "task_status": task_status,
        },
        headers={"Cache-Control": "no-store"},
    )
