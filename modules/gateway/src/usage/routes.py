"""Usage REST API routes."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext
from src.usage.config import AggregationInterval
from src.usage.schemas import (
    UsageByModelResponse,
    UsageByOrganizationResponse,
    UsageListResponse,
    UsageTimelineResponse,
)
from src.usage.service import UsageService

router = APIRouter(prefix="/usage", tags=["usage"])


# Dependency to get current user context
async def get_current_user() -> TokenContext:
    """Get the current authenticated user context.

    Note: In production, this would be populated by auth middleware.
    """
    return TokenContext(
        user_id="system",
        org_id="system",
        team_id="system",
        department_id="system",
        account_type="service",
        is_admin=True,
        expires_at=datetime.now(),
    )


async def get_usage_service(db: Annotated[AsyncSession, Depends(get_db)]) -> UsageService:
    """Get usage service instance."""
    return UsageService(db)


async def get_access_control(db: Annotated[AsyncSession, Depends(get_db)]) -> AccessControl:
    """Get access control instance."""
    return AccessControl(db)


@router.get("/summary")
async def get_usage_summary(
    service: Annotated[UsageService, Depends(get_usage_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    org_id: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    department_id: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """
    Get usage summary.

    Platform admins can query any organization or all organizations.
    Org admins can only query their own organization.
    """
    await access.check_permission(current_user, Permission.USAGE_READ, target_org_id=org_id)

    # Determine which org to query
    accessible_orgs = await access.get_accessible_organizations(current_user)
    if accessible_orgs is not None and not org_id:
        if len(accessible_orgs) > 0:
            org_id = accessible_orgs[0]
        else:
            return {"error": "No accessible organizations"}

    if not org_id:
        # Platform admin without org_id - would need to aggregate across all orgs
        # For simplicity, require org_id
        return {"error": "org_id required"}

    filters: dict[str, Any] = {}
    if start_date:
        filters["start_date"] = start_date
    if end_date:
        filters["end_date"] = end_date
    if department_id:
        filters["department_id"] = department_id
    if model:
        filters["model"] = model

    return await service.get_usage_summary(org_id, filters)


@router.get("/organizations", response_model=list[UsageByOrganizationResponse])
async def get_usage_by_organization(
    service: Annotated[UsageService, Depends(get_usage_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> list[UsageByOrganizationResponse]:
    """
    Get usage by organization.

    Platform admins see all organizations.
    Org admins see only their organization.
    """
    await access.check_permission(current_user, Permission.USAGE_READ)

    # Get accessible organizations
    accessible_orgs = await access.get_accessible_organizations(current_user)

    return await service.get_usage_by_organization(accessible_orgs, start_date, end_date)


@router.get("/organizations/{org_id}")
async def get_organization_usage(
    org_id: str,
    service: Annotated[UsageService, Depends(get_usage_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> dict[str, Any]:
    """Get usage for a specific organization."""
    await access.check_permission(current_user, Permission.USAGE_READ, target_org_id=org_id)

    filters: dict[str, Any] = {}
    if start_date:
        filters["start_date"] = start_date
    if end_date:
        filters["end_date"] = end_date

    return await service.get_usage_summary(org_id, filters)


@router.get("/models", response_model=list[UsageByModelResponse])
async def get_usage_by_model(
    service: Annotated[UsageService, Depends(get_usage_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    org_id: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> list[UsageByModelResponse]:
    """
    Get usage by model.

    Platform admins can query any organization or all.
    Org admins can only query their own organization.
    """
    await access.check_permission(current_user, Permission.USAGE_READ, target_org_id=org_id)

    # Determine org filter
    accessible_orgs = await access.get_accessible_organizations(current_user)
    if accessible_orgs is not None:
        if org_id and org_id not in accessible_orgs:
            from src.admin.exceptions import AccessDeniedError

            raise AccessDeniedError("Cannot access usage from other organizations")
        if not org_id and len(accessible_orgs) > 0:
            org_id = accessible_orgs[0]

    return await service.get_usage_by_model(org_id, start_date, end_date)


@router.get("/timeline", response_model=UsageTimelineResponse)
async def get_usage_timeline(
    service: Annotated[UsageService, Depends(get_usage_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    org_id: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    interval: AggregationInterval = AggregationInterval.DAILY,
) -> UsageTimelineResponse:
    """
    Get usage timeline data.

    Returns time-series data for charting.
    """
    await access.check_permission(current_user, Permission.USAGE_READ, target_org_id=org_id)

    # Determine org filter
    accessible_orgs = await access.get_accessible_organizations(current_user)
    if accessible_orgs is not None:
        if org_id and org_id not in accessible_orgs:
            from src.admin.exceptions import AccessDeniedError

            raise AccessDeniedError("Cannot access usage from other organizations")
        if not org_id and len(accessible_orgs) > 0:
            org_id = accessible_orgs[0]

    return await service.get_usage_timeline(org_id, start_date, end_date, interval)


@router.get("/users", response_model=UsageListResponse)
async def get_usage_by_user(
    service: Annotated[UsageService, Depends(get_usage_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    org_id: str,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> UsageListResponse:
    """Get usage by user for an organization."""
    await access.check_permission(current_user, Permission.USAGE_READ, target_org_id=org_id)

    users = await service.get_usage_by_user(org_id, start_date, end_date, limit)

    return UsageListResponse(
        items=users,
        total=len(users),
        page=1,
        page_size=limit,
        has_more=False,  # Simplified pagination
    )


@router.get("/departments", response_model=UsageListResponse)
async def get_usage_by_department(
    service: Annotated[UsageService, Depends(get_usage_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    org_id: str,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> UsageListResponse:
    """Get usage by department for an organization."""
    await access.check_permission(current_user, Permission.USAGE_READ, target_org_id=org_id)

    departments = await service.get_usage_by_department(org_id, start_date, end_date)

    return UsageListResponse(
        items=departments,
        total=len(departments),
        page=1,
        page_size=len(departments),
        has_more=False,
    )


@router.get("/logs", response_model=UsageListResponse)
async def get_usage_logs(
    service: Annotated[UsageService, Depends(get_usage_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    org_id: str,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    user_id: str | None = None,
    model: str | None = None,
    status_code: int | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UsageListResponse:
    """
    Get raw usage logs for an organization.

    Supports filtering by user, model, status code, and time range.
    """
    await access.check_permission(current_user, Permission.LOGS_READ, target_org_id=org_id)

    filters: dict[str, Any] = {}
    if start_date:
        filters["start_date"] = start_date
    if end_date:
        filters["end_date"] = end_date
    if user_id:
        filters["user_id"] = user_id
    if model:
        filters["model"] = model
    if status_code:
        filters["status_code"] = status_code

    logs = await service.query_logs(org_id, filters, limit, offset)

    return UsageListResponse(
        items=logs,
        total=len(logs),  # Would need a separate count query for accurate total
        page=(offset // limit) + 1,
        page_size=limit,
        has_more=len(logs) == limit,
    )
