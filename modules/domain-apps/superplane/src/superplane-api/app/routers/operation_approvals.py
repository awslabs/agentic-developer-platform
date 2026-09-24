"""Human approval of immutable requests; these routes never dispatch work."""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.database import async_session_factory
from app.services.operation_approvals import ApprovalDenied, ApprovalService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/operation-approvals", tags=["operation-approvals"])


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str = Field(min_length=1, max_length=255)
    action: str = Field(pattern="^(provision|teardown)$")
    idempotency_key: str = Field(min_length=1, max_length=255)
    parameters: dict[str, str]


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result: str = Field(pattern="^(allowed-once|rejected)$")


async def _call(action):
    try:
        return await action
    except ApprovalDenied as error:
        raise HTTPException(403, str(error)) from None
    except Exception as error:
        logger.warning("approval store unavailable (%s)", type(error).__name__)
        raise HTTPException(503, "operation approval unavailable") from None


@router.post("")
async def request_approval(body: ApprovalRequest):
    from harness_jobs.identity import ContractViolation, OperationRequest

    try:
        request = OperationRequest(
            action=body.action,
            idempotency_key=body.idempotency_key,
            parameters=body.parameters,
        )
    except ContractViolation:
        raise HTTPException(422, "invalid operation request") from None
    return await _call(
        ApprovalService(async_session_factory).issue(
            workspace_id=body.workspace_id, request=request
        )
    )


@router.get("/{approval_id}")
async def get_approval(approval_id: str):
    return await _call(ApprovalService(async_session_factory).read(approval_id))


@router.post("/{approval_id}/decision")
async def decide_approval(approval_id: str, body: ApprovalDecision):
    return await _call(
        ApprovalService(async_session_factory).decide(approval_id, body.result)
    )
