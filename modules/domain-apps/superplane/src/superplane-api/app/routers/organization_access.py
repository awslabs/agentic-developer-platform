"""Versioned organization access reads, independent of legacy organization APIs."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas.organization_access import OrganizationAccessResponse, OrganizationAssignmentsResponse
from app.services.organization_access import list_organization_assignments, read_my_organization_access

router = APIRouter(prefix="/orgs/current/access/v1", tags=["organization-access"])


def _caller(request: Request):
    caller = getattr(request.state, "caller", None)
    if caller is None:
        raise HTTPException(403, "verified human organization identity required")
    return caller


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
