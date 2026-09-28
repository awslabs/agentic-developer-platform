"""Dormant cleanup-access routes; mount only after complete retirement composition.

The exact external cleanup authority and separate allocation settlement must be
implemented before adding this router to main, the authorization route inventory,
or the Gateway allowlist.
"""

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.middleware.auth import get_current_org
from app.routers.retirement import RetirementAdmission, RetirementPreview, _call

router = APIRouter(tags=["workspaces"])


@router.post("/workspaces/{workspace_id}/retirement/access/preview")
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


@router.post("/workspaces/{workspace_id}/retirement/access")
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
