"""Public retirement preview and exact human-approved admission."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.middleware.auth import get_current_org
from app.database import get_session
from app.services.provisioning import ProvisioningRefused, ProvisioningUnavailable
from app.services.retirement import admit_retirement, preview_retirement

router = APIRouter(tags=["workspaces"])


class RetirementPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: uuid.UUID


class RetirementAdmission(RetirementPreview):
    plan_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    approval_id: str = Field(min_length=1, max_length=255)


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


@router.post("/workspaces/{workspace_id}/retirement/preview")
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


@router.post("/workspaces/{workspace_id}/retirement")
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
