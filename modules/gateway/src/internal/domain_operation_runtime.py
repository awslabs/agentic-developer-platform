"""Real TokenReview/run/bootstrap authority for deterministic paid domain workers."""

from __future__ import annotations

import json
import os
import ssl
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import httpx
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from src.agentauth.adapter import CREDENTIAL_HEADER
from src.agentauth.bootstrap import BootstrapRefusedError, BootstrapStore
from src.agentauth.grants import AUTHORITY_PAID_DOMAIN_OPERATION
from src.agentauth.routes import AgentRuntime
from src.agentauth.run_credential import CredentialError, verify_credential
from src.agentauth.task_delivery import TaskDelivery
from src.agentauth.workload import WORKLOAD_HEADER, KubernetesWorkloadVerifier
from src.internal.domain_operation_dispatch import paid_operation
from src.internal.domain_operation_store import aws_client, binding_for, bindings, operation_connect

PRODUCER_SCOPE = "domain:operation-producer"
EXECUTOR_SCOPE = "domain:operation-executor"
RECOVERY_SCOPE = "domain:operation-recovery"


def current_registry(request, scope):
    from src.auth.agent_registry import get_agent_registry_service, parse_assumed_role_arn
    from src.internal.auth_deps import INTERNAL_PLANE_SCOPES

    context = getattr(request.state, "token_context", None)
    role = parse_assumed_role_arn(request.headers.get("X-Caller-Identity", ""))
    registry_id = getattr(context, "agent_registry_id", "")
    if getattr(context, "auth_source", None) != "iam" or not role or not registry_id:
        raise HTTPException(403, "domain operation principal refused")
    row = get_agent_registry_service().get_current_agent(registry_id, role)
    if row is None or row.get("scope") not in INTERNAL_PLANE_SCOPES or scope not in row.get("credential_scopes", []):
        raise HTTPException(403, "domain operation capability refused")
    return registry_id


def bootstrap_store():
    table = os.environ.get("AGENT_AUTHORITY_TABLE", "")
    if not table or not os.environ.get("AGENT_RUN_CREDENTIAL_KEY"):
        raise HTTPException(503, "domain worker authority unavailable")
    return BootstrapStore(table_name=table, dynamodb_client=aws_client("dynamodb"))


def metadata(store, record):
    row = store._read("TENANT#" + record.tenant_id, "EXEC#" + record.invocation_id)
    try:
        value = json.loads(row["domain_operation"]["S"])
        required = {"domain", "operation_id", "job_id", "attempt_id", "org_id", "domain_org_id", "adp_org_id", "workspace_id", "mode"}
        if (
            set(value) != required
            or value["adp_org_id"] != record.tenant_id
            or value["mode"] not in {"execution", "recovery"}
            or value["domain_org_id"] != value["org_id"]
        ):
            raise ValueError("invalid domain metadata")
        return value
    except (KeyError, TypeError, ValueError):
        raise BootstrapRefusedError("protected domain assignment unavailable") from None


async def validate_paid_execution(record, grant, *, store, allow_terminal=False):
    if grant.authority.kind != AUTHORITY_PAID_DOMAIN_OPERATION or grant.principal != record.principal:
        raise BootstrapRefusedError("paid domain authority required")
    original = await run_in_threadpool(metadata, store, record)
    binding = binding_for(original["domain"], original["org_id"])
    if binding.adp_org_id != record.tenant_id or grant.repo_scope != frozenset({binding.repo}):
        raise BootstrapRefusedError("paid domain tenant binding refused")
    operation = await paid_operation(binding, original["operation_id"], require_current=original["mode"] == "execution" and not allow_terminal)
    for key in ("operation_id", "job_id", "attempt_id", "org_id", "workspace_id"):
        if original[key] != operation[key]:
            raise BootstrapRefusedError("original domain admission changed")
    if (
        not allow_terminal
        and original["mode"] == "execution"
        and (
            operation["state"] not in {"pending", "running"}
            or operation["cancel_requested_at"]
            or operation["cleanup_required"]
            or operation["reservation_state"] != "confirmed"
        )
    ):
        raise BootstrapRefusedError("paid domain execution withdrawn")
    return binding, original, operation


@lru_cache(maxsize=64)
def _runtime(serialized):
    value = json.loads(serialized)
    binding = binding_for(value["domain"], value["org_id"])
    ca = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    verifier = KubernetesWorkloadVerifier(
        client=httpx.Client(
            base_url="https://kubernetes.default.svc",
            verify=ssl.create_default_context(cafile=str(ca)),
            timeout=5,
            follow_redirects=False,
            trust_env=False,
        ),
        image_digests=frozenset(binding.worker_image_digests),
        namespace=binding.worker_namespace,
        service_account=binding.worker_service_account,
        container_name=binding.worker_container,
    )
    return AgentRuntime(store=bootstrap_store(), workloads=verifier)


def runtime_for(binding):
    # Config changes select a new verifier; an old mounted image/SA never inherits
    # a changed allowlist because the cache key contains the whole binding.
    from dataclasses import asdict

    return _runtime(json.dumps(asdict(binding), sort_keys=True))


def worker_binding(request):
    registry_id = current_registry(request, EXECUTOR_SCOPE)
    selected = [binding for binding in bindings() if binding.worker_registry_id == registry_id]
    if len(selected) != 1:
        raise HTTPException(403, "domain worker binding unavailable")
    return selected[0]


def tasks(runtime, binding):
    return TaskDelivery(store=runtime.store, sqs=aws_client("sqs"), queue_url=binding.queue_url)


async def authenticated(request, *, mode=None, allow_terminal=False):
    binding = worker_binding(request)
    runtime = runtime_for(binding)
    pod, caller, record, grant = await run_in_threadpool(
        runtime.authenticate, request.headers.get(CREDENTIAL_HEADER, ""), request.headers.get(WORKLOAD_HEADER, "")
    )
    resolved, original, operation = await validate_paid_execution(record, grant, store=runtime.store, allow_terminal=allow_terminal)
    if resolved != binding or (mode is not None and original["mode"] != mode):
        raise HTTPException(403, "domain worker assignment refused")
    if original["mode"] == "recovery":
        if current_registry(request, RECOVERY_SCOPE) != binding.worker_registry_id:
            raise HTTPException(403, "domain recovery capability refused")
    return binding, runtime, caller, record, grant, original, operation


async def maybe_domain_executor(request):
    """Select paid runtime using signed identity + protected metadata, never a label."""
    if not bindings():
        return None
    try:
        caller = verify_credential(request.headers.get(CREDENTIAL_HEADER, ""))
    except CredentialError:
        return None
    store = bootstrap_store()
    raw = await run_in_threadpool(store._read, "TENANT#" + caller.tenant_id, "EXEC#" + caller.invocation_id)
    if not raw or "domain_operation" not in raw:
        return None
    binding, _, caller, _, _, _, _ = await authenticated(request, mode="execution")
    request.state.domain_operation_binding = binding
    return caller.principal, binding.org_id


async def current_claim(request, claim):
    binding, runtime, caller, record, grant, original, _ = await authenticated(request, mode="recovery")
    if (claim.org_id, claim.workspace_id) != (binding.org_id, original["workspace_id"]):
        raise HTTPException(403, "recovery claim scope refused")
    async with operation_connect(binding) as connection:
        row = await connection.fetchrow(
            "SELECT l.* FROM harness_operation_leases l JOIN harness_recovery_claim_bindings b "
            "ON b.operation_id=l.operation_id AND b.fence_token=l.fence_token "
            "WHERE l.operation_id=$1 AND l.org_id=$2 AND l.workspace_id=$3 "
            "AND l.holder=$4 AND l.attempt_id=$5 AND l.fence_token=$6 "
            "AND l.closed_at IS NULL AND l.expires_at>clock_timestamp() "
            "AND l.runtime_deadline>clock_timestamp() AND b.subject=$7 "
            "AND b.org_id=l.org_id AND b.workspace_id=l.workspace_id "
            "AND b.holder=l.holder AND b.attempt_id=l.attempt_id",
            claim.operation_id,
            claim.org_id,
            claim.workspace_id,
            claim.holder,
            claim.attempt_id,
            claim.fence_token,
            caller.principal,
        )
        operation = await paid_operation(binding, claim.operation_id, connection=connection)
    if row is None:
        raise HTTPException(403, "recovery claim expired or replaced")
    if min(row["expires_at"], row["runtime_deadline"], grant.expires_at) <= datetime.now(UTC):
        raise HTTPException(403, "recovery claim expired or replaced")
    return binding, dict(row), operation


async def completed_ack(request):
    """A terminal tombstone can acknowledge a retry but grants no execution."""
    from dataclasses import replace

    from src.agentauth.execution import ExecutionStatus, evaluate_execution_state

    binding = worker_binding(request)
    runtime = runtime_for(binding)
    caller = verify_credential(request.headers.get(CREDENTIAL_HEADER, ""), env=runtime.env)
    record = await run_in_threadpool(runtime.store.authority.load_execution, invocation_id=caller.invocation_id, tenant_id=caller.tenant_id)
    if record is None or record.status != ExecutionStatus.COMPLETED:
        return False
    pod = await run_in_threadpool(runtime.workloads.verify, request.headers.get(WORKLOAD_HEADER, ""))
    # Reuse the exact tenant/attempt/epoch/pod checks for this acknowledgement-only
    # path. The real protected record remains COMPLETED and cannot execute.
    evaluate_execution_state(
        record=replace(record, status=ExecutionStatus.ACTIVE),
        invocation_id=caller.invocation_id,
        tenant_id=caller.tenant_id,
        attempt=caller.attempt,
        credential_epoch=caller.credential_epoch,
        presented_workload_binding=pod.uid,
        now=datetime.now(UTC),
    )
    grant = await run_in_threadpool(
        runtime.store.live_grant,
        invocation_id=record.invocation_id,
        tenant_id=record.tenant_id,
        attempt=record.current_attempt,
        now=datetime.now(UTC),
    )
    resolved, _, _ = await validate_paid_execution(record, grant, store=runtime.store, allow_terminal=True)
    task = await run_in_threadpool(tasks(runtime, binding).read, pod.uid)
    if resolved != binding or task is None or task.get("state") != "acknowledged" or task.get("invocation_id") != record.invocation_id:
        raise HTTPException(403, "domain task acknowledgement refused")
    return True
