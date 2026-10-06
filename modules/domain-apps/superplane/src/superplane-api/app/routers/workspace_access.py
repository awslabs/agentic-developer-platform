"""Versioned, explicit workspace access control for current human members."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas.workspace_access import GrantHumanAccessRequest, WorkspaceAccessResponse
from app.services.workspace_access import grant_human_access, read_my_access

router = APIRouter(prefix="/workspaces/{workspace_id}/access/v1", tags=["workspace-access"])


@router.get("/me", response_model=WorkspaceAccessResponse)
async def my_workspace_access(
    workspace_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> WorkspaceAccessResponse:
    caller = getattr(request.state, "caller", None)
    if caller is None:
        raise HTTPException(403, "verified human workspace identity required")
    return await read_my_access(
        db, workspace_id, caller,
        getattr(request.app.state, "current_identity_reader", None),
    )


@router.post("/grants", response_model=WorkspaceAccessResponse)
async def assign_human_workspace_access(
    workspace_id: uuid.UUID,
    body: GrantHumanAccessRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> WorkspaceAccessResponse:
    caller = getattr(request.state, "caller", None)
    if caller is None:
        raise HTTPException(403, "verified human workspace identity required")
    return await grant_human_access(
        db, workspace_id, caller, body,
        getattr(request.app.state, "current_identity_reader", None),
    )
