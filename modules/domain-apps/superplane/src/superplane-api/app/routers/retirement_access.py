"""Dormant cleanup-access routes; mount only after complete retirement composition.

The exact external cleanup authority and separate allocation settlement must be
implemented before adding this router to main, the authorization route inventory,
or the Gateway allowlist.
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.middleware.auth import get_current_org
from app.routers.retirement import RetirementAdmission, RetirementPreview, _call

router = APIRouter(tags=["workspaces"])


class RetirementAccessReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retirement_request_id: uuid.UUID
    request_id: uuid.UUID
    workspace_id: uuid.UUID
    phase: Literal["prepare-retirement-access"]
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_operation_id: str
    allocation_id: str
    original_allocation_id: str
    inventory_sha256: str
    access_plan: dict
    authority: dict
    preserved: list[str]
    max_resource_units: Literal[0]
    max_cost_micros: Literal[0]
    approval_request: dict
    admission_available: bool = False


class RetirementAccessReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retirement_request_id: uuid.UUID
    request_id: uuid.UUID
    control_operation_id: str
    workspace_id: uuid.UUID
    phase: Literal["prepare-retirement-access"]
    state: str
    retryable: bool
    retirement_complete: Literal[False] = False


@router.post(
    "/workspaces/{workspace_id}/retirement/access/preview",
    response_model=RetirementAccessReviewResponse,
)
async def retirement_access_preview(
    workspace_id: uuid.UUID,
    body: RetirementPreview,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    from app.services.retirement_access import preview_access

    async def invoke(composition):
        return (
            await preview_access(
                composition, db, org_id, workspace_id, body.operation_id
            )
        )[3]

    return await _call(request, invoke)


@router.post(
    "/workspaces/{workspace_id}/retirement/access",
    response_model=RetirementAccessReceipt,
)
async def retirement_access_admission(
    workspace_id: uuid.UUID,
    body: RetirementAdmission,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    from app.services.retirement_access import admit_access

    return await _call(
        request,
        lambda composition: admit_access(
            composition,
            db,
            org_id,
            workspace_id,
            body.operation_id,
            body.plan_revision,
            body.approval_id,
        ),
    )
