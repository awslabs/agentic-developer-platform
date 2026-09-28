"""IRSA-authenticated worker API for faithful persona-model probes."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.persona_model_probe_service import (
    ProbeConflictError,
    claim_probe,
    complete_probe,
    start_probe,
)
from src.shared.config import get_settings
from src.shared.database import get_db
from src.tasks.personas import TASK_PERSONAS

router = APIRouter(prefix="/internal/v1/persona-model-probes", tags=["internal-model-probes"])
SHA256_PATTERN = r"^[0-9a-f]{64}$"
PROBE_WORKER_ID = "persona-model-probe"


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimRequest(StrictBody):
    task_persona: Literal["agent-task-investigator", "agent-task-cyber", "agent-task-claude-developer", "agent-task-codex-developer"] | None = None
    trigger: Literal["scheduled", "manual", "change"] = "scheduled"


class ClaimResponse(BaseModel):
    claimed: bool
    task_probe_json: str | None = None
    reason: str | None = None
    slot_id: str | None = None
    lease_token: str | None = None
    model_id: str | None = None
    compatibility_class: str | None = None
    harness_contract_revision: str | None = None
    expected_request_shape_sha256: str | None = None
    max_budget_usd: Decimal | None = None
    timeout_seconds: int | None = None
    lease_expires_at: datetime | None = None


class StartRequest(StrictBody):
    lease_token: str = Field(min_length=32, max_length=128)
    request_shape_sha256: str = Field(pattern=SHA256_PATTERN)


class StartResponse(BaseModel):
    slot_id: str
    model_id: str
    account_id: str
    region: str
    access_key_id: str
    secret_access_key: str
    session_token: str
    credentials_expires_at: datetime


class CompleteRequest(StrictBody):
    lease_token: str = Field(min_length=32, max_length=128)
    outcome: Literal["proven", "refused", "error"]
    request_shape_sha256: str = Field(pattern=SHA256_PATTERN)
    provider_request_id: str | None = Field(default=None, max_length=128)
    error_code: str | None = Field(default=None, max_length=128)


class CompleteResponse(BaseModel):
    slot_id: str
    status: Literal["completed"]
    evidence_recorded: bool


def _conflict(exc: ProbeConflictError) -> HTTPException:
    return HTTPException(status_code=409, detail={"error": exc.reason, "message": exc.message})


async def verify_model_probe_irsa(
    request: Request,
    x_caller_identity: str | None = Header(default=None),
) -> None:
    """Require the dedicated probe IRSA identity; shared worker identities are forbidden."""
    if not x_caller_identity:
        raise HTTPException(status_code=403, detail={"error": "irsa_required", "message": "Probe routes require IRSA"})
    await verify_internal_or_irsa(
        request,
        x_internal_api_key=None,
        x_caller_identity=x_caller_identity,
    )
    context = getattr(request.state, "token_context", None)
    if (
        context is None
        # The IAM adapter's compatibility ``user_id`` is Agent Registry
        # ``agent_name``: mutable and non-unique.  Bind this credential-bearing
        # route to the immutable seeded primary key as well as its fixed
        # platform metadata so a tenant-created lookalike name fails closed.
        or getattr(context, "agent_registry_id", "") != PROBE_WORKER_ID
        or context.user_id != PROBE_WORKER_ID
        or context.org_id != "__platform__"
        or context.scope != "internal"
    ):
        raise HTTPException(
            status_code=403,
            detail={"error": "probe_worker_required", "message": "Caller is not the registered probe worker"},
        )


@router.post("/claim", response_model=ClaimResponse, dependencies=[Depends(verify_model_probe_irsa)])
async def claim(body: ClaimRequest, db: AsyncSession = Depends(get_db)) -> ClaimResponse:
    result = await claim_probe(db, trigger=body.trigger, task_persona=body.task_persona)
    if not result.claimed or result.slot is None:
        return ClaimResponse(claimed=False, reason=result.reason)
    slot = result.slot
    return ClaimResponse(
        claimed=True,
        task_probe_json=TASK_PERSONAS[body.task_persona].probe_json if body.task_persona else None,
        slot_id=slot.id,
        lease_token=result.lease_token,
        model_id=slot.canonical_model_id,
        compatibility_class=slot.compatibility_class,
        harness_contract_revision=slot.harness_contract_revision,
        expected_request_shape_sha256=slot.expected_request_shape_sha256,
        max_budget_usd=slot.reserved_budget_usd,
        timeout_seconds=get_settings().model_probe_timeout_seconds,
        lease_expires_at=slot.lease_expires_at,
    )


@router.post("/{slot_id}/start", response_model=StartResponse, dependencies=[Depends(verify_model_probe_irsa)])
async def start(slot_id: str, body: StartRequest, db: AsyncSession = Depends(get_db)) -> StartResponse:
    try:
        result = await start_probe(
            db,
            slot_id=slot_id,
            lease_token=body.lease_token,
            request_shape_sha256=body.request_shape_sha256,
        )
    except ProbeConflictError as exc:
        raise _conflict(exc) from exc
    credentials = result.credentials
    return StartResponse(
        slot_id=result.slot.id,
        model_id=result.slot.canonical_model_id,
        account_id=result.slot.account_id,
        region=credentials.region,
        access_key_id=credentials.access_key_id,
        secret_access_key=credentials.secret_access_key,
        session_token=credentials.session_token,
        credentials_expires_at=credentials.expiration,
    )


@router.post("/{slot_id}/complete", response_model=CompleteResponse, dependencies=[Depends(verify_model_probe_irsa)])
async def complete(slot_id: str, body: CompleteRequest, db: AsyncSession = Depends(get_db)) -> CompleteResponse:
    try:
        result = await complete_probe(
            db,
            slot_id=slot_id,
            lease_token=body.lease_token,
            outcome=body.outcome,
            request_shape_sha256=body.request_shape_sha256,
            provider_request_id=body.provider_request_id,
            error_code=body.error_code,
        )
    except ProbeConflictError as exc:
        raise _conflict(exc) from exc
    return CompleteResponse(slot_id=result.slot.id, status="completed", evidence_recorded=result.evidence_recorded)
