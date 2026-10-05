"""Protected paid dispatch, actual pod bootstrap, lease acquisition and recovery."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.agentauth.bootstrap import issue_bound_credential
from src.agentauth.workload import WORKLOAD_HEADER
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.domain_current_identity import current_human_identity
from src.internal.domain_operation_dispatch import dispatch
from src.internal.domain_operation_runtime import (
    PRODUCER_SCOPE,
    authenticated,
    bootstrap_store,
    completed_ack,
    current_claim,
    current_registry,
    runtime_for,
    tasks,
    validate_paid_execution,
    worker_binding,
)
from src.internal.domain_operation_store import aws_client, binding_for, harness, operation_connect, secret
from src.shared.database import get_db


class DomainOperationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def guarded(request):
            from src.agentauth.bootstrap import BootstrapRefusedError
            from src.agentauth.execution import ExecutionStateError
            from src.agentauth.run_credential import CredentialError
            from src.agentauth.task_delivery import TaskDeliveryError
            from src.agentauth.workload import WorkloadRefusedError

            try:
                return await handler(request)
            except (HTTPException, RequestValidationError):
                raise
            except (BootstrapRefusedError, ExecutionStateError, CredentialError, TaskDeliveryError, WorkloadRefusedError):
                raise HTTPException(403, "domain operation authority refused") from None
            except Exception:
                raise HTTPException(503, "domain operation dependency unavailable") from None

        return guarded


router = APIRouter(
    route_class=DomainOperationRoute,
    prefix="/internal/v1/controller-execution",
    tags=["domain-operations"],
    dependencies=[Depends(verify_internal_or_irsa)],
)


class DomainScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: Literal["superplane"]
    org_id: str = Field(min_length=1, max_length=255)


class CurrentIdentityRequest(DomainScope):
    subject: str = Field(min_length=1, max_length=255)
    principal_type: Literal["human", "service"]


class DispatchRequest(DomainScope):
    operation_id: str = Field(min_length=1, max_length=255)
    job_id: str = Field(min_length=1, max_length=255)
    attempt_id: str = Field(min_length=1, max_length=255)
    workspace_id: str = Field(min_length=1, max_length=255)
    mode: Literal["execution", "recovery"] = "execution"


class VerifyRunRequest(DomainScope):
    workspace_id: str = Field(min_length=1, max_length=255)
    operation_id: str = Field(min_length=1, max_length=255)
    subject: str = Field(min_length=1, max_length=255)


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BootstrapRequest(Empty):
    invocation_id: str = Field(min_length=1, max_length=128)
    envelope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class OperationRequest(Empty):
    operation_id: str = Field(min_length=1, max_length=255)


class Claim(Empty):
    operation_id: str = Field(min_length=1, max_length=255)
    org_id: str = Field(min_length=1, max_length=255)
    workspace_id: str = Field(min_length=1, max_length=255)
    holder: str = Field(min_length=1, max_length=255)
    attempt_id: str = Field(min_length=1, max_length=255)
    fence_token: int = Field(strict=True, ge=1)


class ClaimRequest(Empty):
    claim: Claim


class ObserveRequest(ClaimRequest):
    query_id: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=1, max_length=255)


class InventoryRequest(ClaimRequest):
    query_id: str = Field(min_length=1, max_length=255)
    allocation_id: str = Field(min_length=1, max_length=255)


class SettlementRequest(Empty):
    receipt_id: str = Field(min_length=1, max_length=255)
    payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_id: str = Field(min_length=1, max_length=255)
    job_id: str = Field(min_length=1, max_length=255)
    attempt_id: str = Field(min_length=1, max_length=255)
    org_id: str = Field(min_length=1, max_length=255)
    workspace_id: str = Field(min_length=1, max_length=255)
    accounting: dict


def producer(request, body):
    binding = binding_for(body.domain, body.org_id)
    if current_registry(request, PRODUCER_SCOPE) != binding.producer_registry_id:
        raise HTTPException(403, "paid domain producer refused")
    return binding


@router.post("/current-identity")
async def current_identity(body: CurrentIdentityRequest, request: Request, db: AsyncSession = Depends(get_db)):
    binding = producer(request, body)
    if body.principal_type != "human":
        raise HTTPException(403, "current identity refused")
    identity = await current_human_identity(db, subject=body.subject, adp_org_id=binding.adp_org_id)
    if producer(request, body) != binding:
        raise HTTPException(403, "current identity refused")
    return identity


@router.post("/producer-readiness")
async def producer_readiness(body: DomainScope, request: Request):
    binding = producer(request, body)
    async with operation_connect(binding) as connection:
        mapped = await connection.fetchval("SELECT adp_org_id FROM organizations WHERE id::text=$1", binding.org_id)
    if mapped != binding.adp_org_id:
        raise HTTPException(503, "domain tenant mapping unavailable")
    await run_in_threadpool(aws_client("sqs").get_queue_attributes, QueueUrl=binding.queue_url, AttributeNames=["QueueArn"])
    store = bootstrap_store()
    table = await run_in_threadpool(store.client.describe_table, TableName=store.table)
    if table.get("Table", {}).get("TableStatus") != "ACTIVE":
        raise HTTPException(503, "domain worker authority unavailable")
    producer(request, body)
    return {
        "version": 1,
        "ready": True,
        "domain": binding.domain,
        "org_id": binding.org_id,
        "domain_org_id": binding.org_id,
        "adp_org_id": binding.adp_org_id,
    }


@router.post("/dispatch")
async def publish(body: DispatchRequest, request: Request):
    binding = producer(request, body)
    result = await dispatch(binding, body, bootstrap_store(), authorize=lambda: producer(request, body))
    producer(request, body)
    return result


@router.post("/verify-run")
async def verify_run(body: VerifyRunRequest, request: Request):
    from src.agentauth.execution import ExecutionStatus

    binding = producer(request, body)
    invocation, separator, attempt = body.subject.rpartition("#")
    if not separator or not invocation or not attempt.isdecimal():
        raise HTTPException(403, "domain run subject refused")
    runtime = runtime_for(binding)

    async def current():
        record = await run_in_threadpool(runtime.store.authority.load_execution, invocation_id=invocation, tenant_id=binding.adp_org_id)
        if record is None or record.principal != body.subject or record.status != ExecutionStatus.ACTIVE or not record.workload_binding:
            raise HTTPException(403, "domain run unavailable")
        grant = await run_in_threadpool(
            runtime.store.live_grant, invocation_id=invocation, tenant_id=binding.adp_org_id, attempt=record.current_attempt, now=datetime.now(UTC)
        )
        resolved, original, operation = await validate_paid_execution(record, grant, store=runtime.store)
        if (
            resolved != binding
            or original["operation_id"] != body.operation_id
            or original["workspace_id"] != body.workspace_id
            or original["mode"] != "execution"
        ):
            raise HTTPException(403, "domain run assignment refused")
        raw = await run_in_threadpool(runtime.store._read, "TENANT#" + binding.adp_org_id, "EXEC#" + invocation)
        await run_in_threadpool(runtime.workloads.verify_bound, name=raw.get("pod_name", {}).get("S", ""), uid=record.workload_binding)
        return record, grant, original, operation

    record, grant, original, operation = await current()
    async with operation_connect(binding) as connection:
        row = await connection.fetchrow(
            "SELECT * FROM harness_operation_leases WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3 "
            "AND holder=$4 AND attempt_id=$4 AND closed_at IS NULL AND expires_at>clock_timestamp() "
            "AND runtime_deadline>clock_timestamp()",
            body.operation_id,
            body.org_id,
            body.workspace_id,
            body.subject,
        )
    latest, current_grant, _, _ = await current()
    producer(request, body)
    if (
        row is None
        or latest != record
        or current_grant != grant
        or min(row["expires_at"], row["runtime_deadline"], grant.expires_at) <= datetime.now(UTC)
    ):
        raise HTTPException(403, "domain run lease changed")
    return {
        **original,
        **authority_payload(operation, row),
        "admission_attempt_id": original["attempt_id"],
        "invocation_id": invocation,
        "subject": record.principal,
        "permissions": ["workspace:provision"],
        "not_after": grant.expires_at.isoformat(),
    }


@router.post("/task/acquire")
async def acquire_task(body: Empty, request: Request):
    binding = worker_binding(request)
    runtime = runtime_for(binding)
    token = request.headers.get(WORKLOAD_HEADER, "")
    pod = await run_in_threadpool(runtime.workloads.verify, token)
    envelope = await run_in_threadpool(tasks(runtime, binding).acquire, pod.uid)
    if await run_in_threadpool(runtime.workloads.verify, token) != pod:
        raise HTTPException(403, "domain workload changed")
    return {"body": envelope}


@router.post("/bootstrap")
async def bootstrap(body: BootstrapRequest, request: Request):
    binding = worker_binding(request)
    runtime = runtime_for(binding)
    token = request.headers.get(WORKLOAD_HEADER, "")
    pod = await run_in_threadpool(runtime.workloads.verify, token)
    await run_in_threadpool(tasks(runtime, binding).require_assignment, pod.uid, body.invocation_id, body.envelope_digest)
    record = await run_in_threadpool(
        runtime.store.bind, invocation_id=body.invocation_id, digest=body.envelope_digest, pod=pod, now=datetime.now(UTC)
    )
    grant = await run_in_threadpool(
        runtime.store.live_grant,
        invocation_id=record.invocation_id,
        tenant_id=record.tenant_id,
        attempt=record.current_attempt,
        now=datetime.now(UTC),
    )
    resolved, original, _ = await validate_paid_execution(record, grant, store=runtime.store)
    if resolved != binding or await run_in_threadpool(runtime.workloads.verify, token) != pod:
        raise HTTPException(403, "domain bootstrap binding refused")
    # A deterministic paid worker has no model/GitHub/agent-control grant. It uses
    # this explicit authority kind and these bounded domain endpoints only.
    return {**issue_bound_credential(record, now=datetime.now(UTC)), "domain_operation": original, "not_after": grant.expires_at.isoformat()}


@router.post("/task/heartbeat")
async def heartbeat_task(body: Empty, request: Request):
    binding, runtime, caller, record, grant, original, operation = await authenticated(request)
    pod = await run_in_threadpool(runtime.workloads.verify, request.headers.get(WORKLOAD_HEADER, ""))
    await run_in_threadpool(tasks(runtime, binding).maintain, pod.uid, acknowledge=False)
    return {"accepted": True}


@router.post("/renew")
async def renew_run(body: Empty, request: Request):
    binding, runtime, caller, record, grant, original, operation = await authenticated(request)
    await run_in_threadpool(
        runtime.store.authority.rotate_credential_epoch,
        invocation_id=record.invocation_id,
        tenant_id=record.tenant_id,
        expected_epoch=record.current_credential_epoch,
        expected_attempt=record.current_attempt,
        workload_binding=record.workload_binding,
        overlap_seconds=30,
    )
    _, _, _, current, grant, _, _ = await authenticated(request)
    return {**issue_bound_credential(current, now=datetime.now(UTC)), "not_after": grant.expires_at.isoformat()}


@router.post("/task/ack")
async def acknowledge_task(body: Empty, request: Request):
    from src.agentauth.execution import ExecutionStatus

    if await completed_ack(request):
        return {"accepted": True}
    binding, runtime, caller, record, grant, original, operation = await authenticated(request, allow_terminal=True)
    if original["mode"] == "execution" and operation["state"] not in {"succeeded", "failed", "unknown", "cancelled"}:
        raise HTTPException(409, "domain operation remains unfinished")
    await run_in_threadpool(tasks(runtime, binding).maintain, record.workload_binding, acknowledge=True)
    await run_in_threadpool(
        runtime.store.authority.set_execution_status,
        invocation_id=record.invocation_id,
        tenant_id=record.tenant_id,
        status=ExecutionStatus.COMPLETED,
        expected_attempt=record.current_attempt,
        expected_status=ExecutionStatus.ACTIVE,
    )
    return {"accepted": True}


def authority_payload(operation, lease):
    lease_keys = (
        "operation_id",
        "org_id",
        "workspace_id",
        "holder",
        "attempt_id",
        "fence_token",
        "expires_at",
        "acquired_at",
        "runtime_deadline",
        "attempts",
        "max_attempts",
    )
    operation_keys = ("job_id", "plan_digest", "request_payload", "reservation_state", "max_resource_units", "max_runtime_seconds", "max_cost_micros")
    return {"version": 1, **{key: lease[key] for key in lease_keys}, **{key: operation[key] for key in operation_keys}}


@router.post("/lease")
async def acquire_lease(body: OperationRequest, request: Request):
    binding, runtime, caller, record, grant, original, operation = await authenticated(request, mode="execution")
    if body.operation_id != original["operation_id"]:
        raise HTTPException(403, "domain lease assignment refused")
    async with operation_connect(binding) as connection:
        async with connection.transaction():
            await connection.fetchrow("SELECT operation_id FROM harness_operations WHERE operation_id=$1 FOR UPDATE", body.operation_id)
            row = await connection.fetchrow("SELECT * FROM harness_operation_leases WHERE operation_id=$1", body.operation_id)
            if row is not None and row["holder"] == caller.principal and row["attempt_id"] == caller.principal:
                if row["closed_at"] is not None or min(row["expires_at"], row["runtime_deadline"]) <= datetime.now(UTC):
                    raise HTTPException(403, "expired domain lease requires recovery")
                fence_token = row["fence_token"]
            else:
                lease = await harness("leases").acquire(
                    connection, operation_id=body.operation_id, holder=caller.principal, attempt_id=caller.principal
                )
                fence_token = lease.fence_token
            # A lost response reuses the exact fence; it never renews or reacquires.
            await connection.execute(
                "UPDATE harness_operation_leases SET expires_at=LEAST(expires_at,$2),"
                "runtime_deadline=LEAST(runtime_deadline,$2) "
                "WHERE operation_id=$1 AND holder=$3 AND fence_token=$4",
                body.operation_id,
                grant.expires_at,
                caller.principal,
                fence_token,
            )
            row = dict(await connection.fetchrow("SELECT * FROM harness_operation_leases WHERE operation_id=$1", body.operation_id))
    await authenticated(request, mode="execution")
    return {**authority_payload(operation, row), "adp_org_id": binding.adp_org_id, "not_after": grant.expires_at.isoformat()}


@router.post("/task/status")
async def task_status(body: Empty, request: Request):
    _, _, _, _, _, original, operation = await authenticated(request, allow_terminal=True)
    return {"operation_id": original["operation_id"], "state": operation["state"], "cancelled": operation["cancel_requested_at"] is not None}


@router.post("/recovery/scope")
async def recovery_scope(body: Empty, request: Request):
    binding, runtime, caller, record, grant, original, operation = await authenticated(request, mode="recovery")
    return {
        "version": 1,
        "observation_only": True,
        "operation_type": "workspace_lifecycle"
        if "runtime_config_sha256" in harness("identity").decode_payload(operation["request_payload"]).parameters
        else "controller",
        "org_id": binding.org_id,
        "adp_org_id": binding.adp_org_id,
        "workspace_id": original["workspace_id"],
        "subject": caller.principal,
        "permissions": ["workspace:recover"],
        "not_after": min(grant.expires_at, caller.expires_at).isoformat(),
    }


@router.post("/recovery/authority")
async def recovery_authority(body: ClaimRequest, request: Request):
    binding, lease, operation = await current_claim(request, body.claim)
    return {**authority_payload(operation, lease), "claim": body.claim.model_dump(), "observation_only": True, "adp_org_id": binding.adp_org_id}


async def domain_request(binding, action, payload):
    credential = (await secret(binding.observation_credential_secret_id)).strip()
    async with httpx.AsyncClient(timeout=25, follow_redirects=False, trust_env=False) as client:
        response = await client.post(
            binding.observation_url.rstrip("/") + "/internal/controller/recovery/" + action, headers={"Authorization": credential}, json=payload
        )
    if response.status_code != 200 or len(response.content) > 262144:
        raise HTTPException(503, "domain recovery observation unavailable")
    return response.json()


async def observation(body, request, action):
    binding, _, _ = await current_claim(request, body.claim)
    result = await domain_request(binding, action, body.model_dump())
    await current_claim(request, body.claim)
    if not isinstance(result, dict) or result.get("claim") != body.claim.model_dump() or result.get("query_id") != body.query_id:
        raise HTTPException(503, "domain recovery observation binding unavailable")
    return {**result, "version": 1, "observation_only": True}


@router.post("/recovery/observe")
async def observe(body: ObserveRequest, request: Request):
    return await observation(body, request, "observe")


@router.post("/recovery/inventory")
async def inventory(body: InventoryRequest, request: Request):
    return await observation(body, request, "inventory")


@router.post("/recovery/lifecycle")
async def lifecycle(body: ObserveRequest, request: Request):
    return await observation(body, request, "lifecycle")


@router.post("/recovery/account-creation")
async def account_creation(body: ObserveRequest, request: Request):
    return await observation(body, request, "account-creation")


@router.post("/recovery/bootstrap")
async def bootstrap_recovery(body: ObserveRequest, request: Request):
    return await observation(body, request, "bootstrap")


@router.post("/recovery/settlement")
async def settlement(body: SettlementRequest, request: Request):
    binding, _, _, _, _, original, _ = await authenticated(request, mode="recovery")
    if (body.org_id, body.workspace_id) != (binding.org_id, original["workspace_id"]):
        raise HTTPException(403, "settlement tenant refused")
    result = await domain_request(binding, "settlement", body.model_dump())
    if result != {"receipt_id": body.receipt_id}:
        raise HTTPException(503, "settlement acknowledgement unavailable")
    return result
