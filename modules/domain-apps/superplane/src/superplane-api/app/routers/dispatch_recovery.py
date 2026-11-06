"""Workspace-scoped original-request reauthorization; no generic operation reset."""

import uuid

from fastapi import APIRouter, HTTPException, Request
from harness_jobs.identity import OperationRefused
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.operation_dispatch import ProducerRefusedError
from app.database import async_session_factory
from app.routers.onboarding import _composition
from app.services.dispatch_recovery import DispatchRecoveryService
from app.services.operation_approvals import ApprovalDenied

router = APIRouter(
    prefix="/workspaces/{workspace_id}/dispatch-recovery", tags=["workspace-recovery"]
)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(strict=True, ge=1)
    idempotency_key: uuid.UUID


class Resume(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reauthorization_id: uuid.UUID


class Decision(Resume):
    result: str = Field(pattern="^(allowed-once|rejected)$")


async def call(action):
    try:
        return await action
    except (ApprovalDenied, ProducerRefusedError):
        raise HTTPException(403, "current recovery authority refused") from None
    except OperationRefused as error:
        raise HTTPException(409, str(error)) from None
    except Exception:
        raise HTTPException(
            503, "original operation recovery unavailable; retain request identity"
        ) from None


def service(request):
    return DispatchRecoveryService(async_session_factory, _composition(request))


@router.post("/review")
async def review(workspace_id: uuid.UUID, body: Review, request: Request):
    return await call(
        service(request).request(
            str(workspace_id),
            expected_version=body.expected_version,
            idempotency_key=str(body.idempotency_key),
        )
    )


@router.post("/decision")
async def decide(workspace_id: uuid.UUID, body: Decision, request: Request):
    return await call(
        service(request).decide(
            str(workspace_id), str(body.reauthorization_id), body.result
        )
    )


@router.post("/resume")
async def resume(workspace_id: uuid.UUID, body: Resume, request: Request):
    return await call(
        service(request).resume(str(workspace_id), str(body.reauthorization_id))
    )
