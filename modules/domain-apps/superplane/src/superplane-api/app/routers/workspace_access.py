"""Versioned, explicit workspace access control for current human members."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas.workspace_access import GrantHumanAccessRequest, RevokeHumanAccessRequest, WorkspaceAccessResponse, WorkspaceAssignmentsResponse, WorkspaceRevocationResponse
from app.services.workspace_access import grant_human_access, list_workspace_assignments, read_my_access, revoke_human_access

router = APIRouter(prefix="/workspaces/{workspace_id}/access/v1", tags=["workspace-access"])


@router.post("/grants/{grant_id}/revoke", response_model=WorkspaceRevocationResponse)
async def revoke_human_workspace_access(
    workspace_id: uuid.UUID,
    grant_id: uuid.UUID,
    body: RevokeHumanAccessRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> WorkspaceRevocationResponse:
    caller = getattr(request.state, "caller", None)
    if caller is None:
        raise HTTPException(403, "verified human workspace identity required")
    return await revoke_human_access(
        db, workspace_id, grant_id, caller, body,
        getattr(request.app.state, "current_identity_reader", None),
    )


@router.get("/grants", response_model=WorkspaceAssignmentsResponse)
async def workspace_assignments(
    workspace_id: uuid.UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    after: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_session),
) -> WorkspaceAssignmentsResponse:
    caller = getattr(request.state, "caller", None)
    if caller is None:
        raise HTTPException(403, "verified human workspace identity required")
    return await list_workspace_assignments(
        db, workspace_id, caller,
        getattr(request.app.state, "current_identity_reader", None),
        limit=limit, after=after,
    )


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
