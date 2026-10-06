"""Public retirement preview and exact human-approved admission."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.middleware.auth import get_current_org
from app.services.provisioning import ProvisioningRefused, ProvisioningUnavailable
from app.services.retirement import admit_retirement, preview_retirement

router = APIRouter(tags=["workspaces"])


class RetirementPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: uuid.UUID


class RetirementAdmission(RetirementPreview):
    plan_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    approval_id: str = Field(min_length=1, max_length=255)


class RetirementReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    workspace_id: uuid.UUID
    source_operation_id: str
    source_payload_digest: str
    lifecycle_artifact_id: str
    account_id: str
    region: str
    inventory_sha256: str
    lifecycle_policy_sha256: str
    runtime_config_sha256: str
    steps: list[dict]
    preserved: list[str]
    admission_available: bool
    blocked_reason: str | None = None
    approval_request: dict | None = None
    revision: str


class RetirementAdmissionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    workspace_id: uuid.UUID
    operation_id: str
    phase: Literal["retire-workspace"]
    state: str
    retryable: bool
    retirement_complete: Literal[False]
    original_allocation_id: str
    control_allocation_id: str


async def _call(request, invoke):
    from superplane_bootstrap.errors import BootstrapRefused

    composition = getattr(request.app.state, "trust_composition", None)
    if composition is None:
        raise HTTPException(503, "retirement operation authority unavailable")
    try:
        return await invoke(composition)
    except (ProvisioningRefused, BootstrapRefused):
        raise HTTPException(403, "retirement ownership or approval refused") from None
    except ProvisioningUnavailable:
        raise HTTPException(
            503,
            "complete retirement plan is unavailable; no new deletion was submitted",
        ) from None
    except Exception:
        raise HTTPException(
            503, "retirement status unavailable; recover the same request identity"
        ) from None


@router.post(
    "/workspaces/{workspace_id}/retirement/preview",
    response_model=RetirementReviewResponse,
)
async def retirement_preview(
    workspace_id: uuid.UUID,
    body: RetirementPreview,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    async def invoke(composition):
        return (
            await preview_retirement(
                composition, db, org_id, workspace_id, body.operation_id
            )
        )[3]

    return await _call(request, invoke)


@router.post(
    "/workspaces/{workspace_id}/retirement",
    response_model=RetirementAdmissionResponse,
)
async def retirement_admission(
    workspace_id: uuid.UUID,
    body: RetirementAdmission,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    return await _call(
        request,
        lambda composition: admit_retirement(
            composition,
            db,
            org_id,
            workspace_id,
            body.operation_id,
            body.plan_revision,
            body.approval_id,
        ),
    )
