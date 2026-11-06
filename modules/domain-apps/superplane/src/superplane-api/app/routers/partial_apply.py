"""Preparation approval for the existing partially provisioned workspace."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from workspace_provisioning.runtime_config import LifecycleRefused

from app.adapters.operation_dispatch import ProducerRefusedError
from app.database import get_session
from app.database import async_session_factory
from app.middleware.auth import get_current_org
from app.routers.onboarding import _composition
from app.routers.dispatch_recovery import (
    Review,
    Decision,
    Resume,
    call as recovery_call,
)
from app.services.partial_apply_preparation import admit, preview
from app.services.provisioning import ProvisioningRefused
from app.services.partial_apply_repair import PartialApplyRepairService

router = APIRouter(
    prefix="/workspaces/{workspace_id}/partial-apply", tags=["workspace-recovery"]
)


class PreparationPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_evidence_id: str | None = Field(default=None, pattern=r"^sppa1:[a-f0-9]{64}$")


class Admission(PreparationPreview):
    request_id: uuid.UUID
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    approval_id: uuid.UUID


class RepairReview(Review):
    artifact_id: str = Field(pattern=r"^[a-f0-9]{64}$")


async def call(action):
    try:
        return await action
    except (LifecycleRefused, ProvisioningRefused, ProducerRefusedError):
        raise HTTPException(
            409, "partial apply ownership, evidence or approval refused"
        ) from None


@router.post("/preparation/preview")
async def preview_preparation(
    workspace_id: uuid.UUID,
    request: Request,
    body: PreparationPreview | None = None,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    return (
        await call(
            preview(
                _composition(request),
                db,
                org_id,
                workspace_id,
                owner_evidence_id=body.owner_evidence_id if body else None,
            )
        )
    )[-1]


@router.post("/preparation")
async def admit_preparation(
    workspace_id: uuid.UUID,
    body: Admission,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    return await call(
        admit(
            _composition(request),
            db,
            org_id,
            workspace_id,
            str(body.request_id),
            body.revision,
            str(body.approval_id),
            owner_evidence_id=body.owner_evidence_id,
        )
    )


def service(request):
    return PartialApplyRepairService(async_session_factory, _composition(request))


@router.post("/repair/review")
async def review_repair(workspace_id: uuid.UUID, body: RepairReview, request: Request):
    return await recovery_call(
        service(request).request(
            str(workspace_id),
            expected_version=body.expected_version,
            idempotency_key=str(body.idempotency_key),
            artifact_id=body.artifact_id,
        )
    )


@router.post("/repair/decision")
async def decide_repair(workspace_id: uuid.UUID, body: Decision, request: Request):
    return await recovery_call(
        service(request).decide(
            str(workspace_id), str(body.reauthorization_id), body.result
        )
    )


@router.post("/repair/resume")
async def resume_repair(workspace_id: uuid.UUID, body: Resume, request: Request):
    return await recovery_call(
        service(request).resume(str(workspace_id), str(body.reauthorization_id))
    )
