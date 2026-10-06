"""Versioned organization access reads, independent of legacy organization APIs."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas.organization_access import AssignOrganizationAccessRequest, RevokeOrganizationAccessRequest, OrganizationAccessResponse, OrganizationAssignmentsResponse
from app.services.organization_access import list_organization_assignments, read_my_organization_access, mutate_organization_access

router = APIRouter(prefix="/orgs/current/access/v1", tags=["organization-access"])


def _caller(request: Request):
    caller = getattr(request.state, "caller", None)
    if caller is None:
        raise HTTPException(403, "verified human organization identity required")
    return caller


@router.post("/grants", response_model=OrganizationAccessResponse)
async def assign_organization_access(body: AssignOrganizationAccessRequest, request: Request, db: AsyncSession = Depends(get_session)):
    return await mutate_organization_access(db, _caller(request), getattr(request.app.state, "current_identity_reader", None), body)


@router.post("/grants/{grant_id}/revoke", response_model=OrganizationAccessResponse)
async def revoke_organization_access(grant_id: uuid.UUID, body: RevokeOrganizationAccessRequest, request: Request, db: AsyncSession = Depends(get_session)):
    return await mutate_organization_access(db, _caller(request), getattr(request.app.state, "current_identity_reader", None), body, grant_id=grant_id)


@router.get("/me", response_model=OrganizationAccessResponse)
async def my_organization_access(request: Request, db: AsyncSession = Depends(get_session)):
    return await read_my_organization_access(
        db, _caller(request), getattr(request.app.state, "current_identity_reader", None),
    )


@router.get("/grants", response_model=OrganizationAssignmentsResponse)
async def organization_assignments(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    after: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_session),
):
    return await list_organization_assignments(
        db, _caller(request), getattr(request.app.state, "current_identity_reader", None),
        limit=limit, after=after,
    )
