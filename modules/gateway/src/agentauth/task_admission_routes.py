"""Producer-authenticated Task ingress backed by the durable admission service."""

from __future__ import annotations

import os
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from src.agentauth.work_routes import verify_producer
from src.auth.agent_registry import parse_assumed_role_arn
from src.auth.caller_provenance import verified_caller_identity
from src.internal.task_admission_proof import TaskAdmissionProofError, binding_digest, verify_admission_binding
from src.shared.database import get_db
from src.tasks import authz, errors, http
from src.tasks.records import canonical_json

router = APIRouter(prefix="/internal/v1/tasks", tags=["task-api"])
_ADMISSION = None


def require_task_admission_transport(request: Request, *, allowed_roles: set[str]) -> str:
    """Authenticate this one producer route without granting generic agent scope."""
    identity = verified_caller_identity(request)
    role = parse_assumed_role_arn(identity) if identity else None
    if not role or role not in allowed_roles:
        raise HTTPException(403, "forbidden")
    return role


class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["1.0"]
    persona: Annotated[StrictStr, Field(max_length=64, pattern=r"^agent-task-[a-z0-9]+(-[a-z0-9]+)*$")]
    instructions: Annotated[StrictStr, Field(min_length=1, max_length=16000)]
    inputs: dict[str, Any] = Field(default_factory=dict)
    artifact_ids: list[Annotated[StrictStr, Field(pattern=r"^art_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")]] = Field(
        default_factory=list, max_length=4
    )
    external_reference: Annotated[StrictStr, Field(max_length=256)] = ""
    acceptance_criteria: list[Annotated[StrictStr, Field(min_length=1, max_length=1000)]] = Field(default_factory=list, max_length=10)

    @field_validator("artifact_ids")
    @classmethod
    def unique_artifacts(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Duplicate artifact IDs")
        return value


def get_admission():
    global _ADMISSION
    if _ADMISSION is None:
        from src.agentauth.task_admission import TaskAdmission
        from src.tasks.store import TaskStore

        _ADMISSION = TaskAdmission(TaskStore())
    return _ADMISSION


@router.post("/admit")
@http.contract_errors
async def admit(request: Request, db: AsyncSession = Depends(get_db)):
    http.require_flag("ADP_TASK_API_ADMISSION_ENABLED")
    roles = {role.strip() for role in os.environ.get("ADP_TASK_ADMISSION_PRODUCER_ROLES", "").split(",") if role.strip()}
    if not roles:
        raise errors.prerequisite_unavailable("Task ingress producer authorization is not configured.")
    try:
        transport_role = require_task_admission_transport(request, allowed_roles=roles)
    except HTTPException:
        raise errors.disallowed_scope("Task admission requires authenticated internal transport.") from None
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 96 * 1024:
            raise errors.payload_too_large("Task admission request exceeds its fixed byte bound.")
    token = request.headers.get("X-Adp-Task-Caller-Token", "")
    proof = request.headers.get("X-Adp-Producer-Proof", "")
    if not token or len(token) > 4096:
        raise errors.invalid_request("Forwarded caller credential exceeds its contract bound.")
    try:
        payload, public_bytes = verify_admission_binding(bytes(raw), caller_token_header=token, producer_proof_header=proof)
    except TaskAdmissionProofError:
        raise errors.invalid_request("Task admission proof does not match its request.") from None
    if len(public_bytes) > 65536:
        raise errors.payload_too_large("Task submit body exceeds its fixed byte bound.")
    if not 1 <= len(payload["idempotency_key"]) <= 128 or any(not 32 <= ord(char) <= 126 for char in payload["idempotency_key"]):
        raise errors.invalid_request("Task idempotency key must be printable ASCII, at most128 characters.")
    try:
        producer_role = await verify_producer(
            proof,
            binding_digest(method="POST", route="/v1/tasks", caller_token=token, idempotency_key=payload["idempotency_key"], body=public_bytes),
            allowed_roles=roles,
        )
        if producer_role != transport_role:
            raise HTTPException(403, "forbidden")
    except HTTPException:
        raise errors.disallowed_scope("Task admission producer is not authorized.") from None
    try:
        validated = SubmitRequest.model_validate(payload["submit"])
        submit = validated.model_dump(exclude_unset=True)
        canonical_json(submit)
    except (ValidationError, ValueError):
        raise errors.invalid_request("Task submit body does not match the public contract.") from None
    # The forwarded caller token is validated as a new external credential; the
    # producer's IAM identity can never become the task owner.
    caller_request = Request({**request.scope, "headers": [(b"authorization", ("Bearer " + token).encode("utf-8"))]})
    context, scopes = authz.authenticate(caller_request)
    caller = await authz.resolve_caller(context, scopes, db)
    caller.require("adp-tasks/submit")
    from src.agentauth.model_policy import ModelPolicyError
    from src.agentauth.task_admission import TaskAdmissionError
    from src.agentauth.task_budget import TaskBudgetError
    from src.agentauth.task_service_policy import TaskServicePolicyError
    from src.tasks.store import TaskStoreError

    try:
        receipt = await get_admission().admit(caller=caller, submit=submit, idempotency_key=payload["idempotency_key"], db=db)
    except TaskAdmissionError as exc:
        raise errors.TaskApiError(exc.status, exc.code, "Task admission was refused.") from None
    except (TaskStoreError, TaskBudgetError, TaskServicePolicyError, ModelPolicyError):
        raise errors.prerequisite_unavailable("Task admission could not be confirmed; retry the same idempotency key.") from None
    return http.ok(receipt, status=202)
