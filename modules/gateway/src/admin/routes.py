"""Admin REST API routes.

Issue #133: Security Fix - All admin routes now require valid Cognito JWT authentication.
The mock get_current_user() that returned is_admin=True has been replaced with
real Cognito JWT validation via src.auth.dependencies.get_current_user.
"""

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.agent_onboarding_schemas import (
    AgentOnboardRequest,
    AgentOnboardResponse,
)
from src.admin.agent_onboarding_service import AgentOnboardingService
from src.admin.agent_registry_schemas import (
    AgentRegistryCreateRequest,
    AgentRegistryListResponse,
    AgentRegistryResponse,
    AgentRegistryUpdateRequest,
    AgentUsageResponse,
)
from src.admin.agent_registry_service import AgentRegistryService
from src.admin.agent_schemas import (
    AgentCreateRequest,
    AgentCredentialsResponse,
    AgentListResponse,
    AgentResponse,
    AgentUpdateRequest,
)
from src.admin.agent_service import AgentService
from src.admin.cognito_service import CognitoService
from src.admin.config import Permission
from src.admin.log_service import LogService
from src.admin.policy_scoping_schemas import (
    AgentTypesListResponse,
    PolicyPreviewRequest,
    PolicyPreviewResponse,
)
from src.admin.schemas import (
    BudgetConfigResponse,
    BudgetConfigUpdateRequest,
    BudgetCreateRequest,
    BudgetListResponse,
    BudgetStatusResponse,
    ChatDetailResponse,
    ChatListResponse,
    CognitoDepartmentListResponse,
    CognitoDepartmentResponse,
    CognitoTeamListResponse,
    CognitoTeamResponse,
    CognitoUserListResponse,
    CognitoUserResponse,
    LogQueryResponse,
    OrganizationCreateRequest,
    OrganizationListResponse,
    OrganizationResponse,
    OrganizationUpdateRequest,
    OrgDashboardResponse,
    PlatformDashboardResponse,
    PoolAccountCreateRequest,
    PoolAccountResponse,
    PoolStatusResponse,
    RateLimitConfigResponse,
    RateLimitConfigUpdateRequest,
    RateLimitCreateRequest,
    RateLimitListResponse,
    UsageTimeseriesResponse,
)
from src.admin.service import AdminService
from src.auth.dependencies import get_current_user  # Issue #133: Real Cognito JWT auth
from src.shared.database import get_db
from src.shared.schemas.admin import (
    DepartmentCreateRequest,
    DepartmentListResponse,
    DepartmentResponse,
    DepartmentUpdateRequest,
    ServiceAccountCreateRequest,
    ServiceAccountListResponse,
    ServiceAccountResponse,
    TeamCreateRequest,
    TeamListResponse,
    TeamResponse,
    TeamUpdateRequest,
    UserCreateRequest,
    UserListResponse,
    UserResponse,
)
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# Issue #133: CRITICAL SECURITY FIX
# The mock get_current_user() that returned is_admin=True for ALL requests
# has been REMOVED. Authentication is now handled by:
#   src.auth.dependencies.get_current_user
# which validates Cognito JWT tokens and extracts real user context from claims.
# This prevents unauthenticated access to admin routes.


async def get_admin_service(db: Annotated[AsyncSession, Depends(get_db)]) -> AdminService:
    """Get admin service instance."""
    from src.budget.service import BudgetService

    budget_service = BudgetService(db)
    return AdminService(db, budget_service=budget_service)


async def get_access_control(db: Annotated[AsyncSession, Depends(get_db)]) -> AccessControl:
    """Get access control instance."""
    return AccessControl(db)


async def get_log_service(db: Annotated[AsyncSession, Depends(get_db)]) -> LogService:
    """Get log service instance."""
    return LogService(db)


def get_cognito_service() -> CognitoService | None:
    """Get Cognito service instance if configured."""
    import os

    pool_id = os.environ.get("BG_COGNITO_USER_POOL_ID") or os.environ.get("COGNITO_USER_POOL_ID")
    if pool_id:
        return CognitoService(user_pool_id=pool_id)
    return None


# Organization Endpoints


@router.post("/organizations", response_model=OrganizationResponse, status_code=201)
async def create_organization(
    request: OrganizationCreateRequest,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> OrganizationResponse:
    """Create a new organization.

    Requires platform admin privileges.
    """
    await access.check_permission(current_user, Permission.ORG_CREATE)
    return await service.create_organization(request)


@router.get("/organizations", response_model=OrganizationListResponse)
async def list_organizations(
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> OrganizationListResponse:
    """List organizations.

    Platform admins see all organizations.
    Org admins see only their organization.
    """
    await access.check_permission(current_user, Permission.ORG_READ)

    # Get accessible organizations
    accessible_orgs = await access.get_accessible_organizations(current_user)

    orgs, total = await service.list_organizations(page, page_size, accessible_orgs)

    return OrganizationListResponse(
        items=orgs,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < total,
    )


@router.get("/organizations/{org_id}", response_model=OrganizationResponse)
async def get_organization(
    org_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> OrganizationResponse:
    """Get organization details."""
    await access.check_permission(current_user, Permission.ORG_READ, target_org_id=org_id)
    return await service.get_organization(org_id)


@router.put("/organizations/{org_id}", response_model=OrganizationResponse)
async def update_organization(
    org_id: str,
    request: OrganizationUpdateRequest,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> OrganizationResponse:
    """Update organization details."""
    await access.check_permission(current_user, Permission.ORG_UPDATE, target_org_id=org_id)
    return await service.update_organization(org_id, request)


@router.delete("/organizations/{org_id}", status_code=204)
async def delete_organization(
    org_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> None:
    """Delete an organization.

    Requires platform admin privileges.
    """
    await access.check_permission(current_user, Permission.ORG_DELETE)
    await service.delete_organization(org_id)


# Budget Configuration Endpoints


@router.get("/organizations/{org_id}/budget/{entity_type}/{entity_id}", response_model=BudgetConfigResponse | None)
async def get_budget_config(
    org_id: str,
    entity_type: str,
    entity_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> BudgetConfigResponse | None:
    """Get budget configuration for an entity."""
    await access.check_permission(current_user, Permission.BUDGET_READ, target_org_id=org_id)
    return await service.get_budget_config(org_id, entity_type, entity_id)


@router.get(
    "/organizations/{org_id}/budgets/{entity_type}/{entity_id}/status",
    response_model=BudgetStatusResponse,
)
async def get_budget_status(
    org_id: str,
    entity_type: str,
    entity_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> BudgetStatusResponse:
    """Get budget status with current spend for an entity."""
    await access.check_permission(current_user, Permission.BUDGET_READ, target_org_id=org_id)
    return await service.get_budget_status(org_id, entity_type, entity_id)


@router.put("/organizations/{org_id}/budget/{entity_type}/{entity_id}", response_model=BudgetConfigResponse | None)
async def update_budget_config(
    org_id: str,
    entity_type: str,
    entity_id: str,
    request: BudgetConfigUpdateRequest,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> BudgetConfigResponse | None:
    """Update budget configuration for an entity."""
    await access.check_permission(current_user, Permission.BUDGET_UPDATE, target_org_id=org_id)
    return await service.update_budget_config(org_id, entity_type, entity_id, request)


# Budget List/Create/Delete Endpoints (Issue #185)


@router.get("/organizations/{org_id}/budgets", response_model=BudgetListResponse)
async def list_budgets(
    org_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    cognito: Annotated[CognitoService | None, Depends(get_cognito_service)],
    entity_type: Annotated[str | None, Query(description="Filter by entity type")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100, alias="limit")] = 20,
) -> BudgetListResponse:
    """List all budget configurations for an organization.

    Issue #185: Returns all budget configs with current usage and utilization percentage.
    Optionally filter by entity_type (org, department, team, user).
    """
    await access.check_permission(current_user, Permission.BUDGET_READ, target_org_id=org_id)
    return await service.get_budgets_list(org_id, entity_type, page, limit, cognito)


@router.post("/organizations/{org_id}/budgets", response_model=BudgetConfigResponse, status_code=201)
async def create_budget(
    org_id: str,
    request: BudgetCreateRequest,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> BudgetConfigResponse:
    """Create a new budget configuration.

    Issue #185: Explicit create endpoint for budget configurations.
    """
    await access.check_permission(current_user, Permission.BUDGET_UPDATE, target_org_id=org_id)
    return await service.create_budget(org_id, request)


@router.delete("/organizations/{org_id}/budget/{entity_type}/{entity_id}/{period_type}", status_code=204)
async def delete_budget(
    org_id: str,
    entity_type: str,
    entity_id: str,
    period_type: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> None:
    """Delete a budget configuration.

    Issue #185: Delete endpoint for budget configurations.
    """
    await access.check_permission(current_user, Permission.BUDGET_UPDATE, target_org_id=org_id)
    await service.delete_budget(org_id, entity_type, entity_id, period_type)


# Rate Limit Configuration Endpoints


@router.get("/organizations/{org_id}/ratelimit/{entity_type}/{entity_id}", response_model=RateLimitConfigResponse | None)
async def get_ratelimit_config(
    org_id: str,
    entity_type: str,
    entity_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> RateLimitConfigResponse | None:
    """Get rate limit configuration for an entity."""
    await access.check_permission(current_user, Permission.RATELIMIT_READ, target_org_id=org_id)
    return await service.get_ratelimit_config(org_id, entity_type, entity_id)


@router.put("/organizations/{org_id}/ratelimit/{entity_type}/{entity_id}", response_model=RateLimitConfigResponse)
async def update_ratelimit_config(
    org_id: str,
    entity_type: str,
    entity_id: str,
    request: RateLimitConfigUpdateRequest,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> RateLimitConfigResponse:
    """Update rate limit configuration for an entity."""
    await access.check_permission(current_user, Permission.RATELIMIT_UPDATE, target_org_id=org_id)
    return await service.update_ratelimit_config(org_id, entity_type, entity_id, request)


# Rate Limit List/Create/Delete Endpoints (Issue #185)


@router.get("/organizations/{org_id}/ratelimits", response_model=RateLimitListResponse)
async def list_ratelimits(
    org_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    entity_type: Annotated[str | None, Query(description="Filter by entity type")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100, alias="limit")] = 20,
) -> RateLimitListResponse:
    """List all rate limit configurations for an organization.

    Issue #185: Returns all rate limit configs for the organization.
    Optionally filter by entity_type (org, department, team, user).
    """
    await access.check_permission(current_user, Permission.RATELIMIT_READ, target_org_id=org_id)
    return await service.get_ratelimits_list(org_id, entity_type, page, limit)


@router.post("/organizations/{org_id}/ratelimits", response_model=RateLimitConfigResponse, status_code=201)
async def create_ratelimit(
    org_id: str,
    request: RateLimitCreateRequest,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> RateLimitConfigResponse:
    """Create a new rate limit configuration.

    Issue #185: Explicit create endpoint for rate limit configurations.
    """
    await access.check_permission(current_user, Permission.RATELIMIT_UPDATE, target_org_id=org_id)
    return await service.create_ratelimit(org_id, request)


@router.delete("/organizations/{org_id}/ratelimit/{entity_type}/{entity_id}", status_code=204)
async def delete_ratelimit(
    org_id: str,
    entity_type: str,
    entity_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> None:
    """Delete a rate limit configuration.

    Issue #185: Delete endpoint for rate limit configurations.
    """
    await access.check_permission(current_user, Permission.RATELIMIT_UPDATE, target_org_id=org_id)
    await service.delete_ratelimit(org_id, entity_type, entity_id)


# Pool Management Endpoints


@router.get("/pool/status", response_model=PoolStatusResponse)
async def get_pool_status(
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> PoolStatusResponse:
    """Get Bedrock pool status.

    Requires platform admin privileges.
    """
    await access.check_permission(current_user, Permission.POOL_READ)
    return await service.get_pool_status()


@router.post("/pool/accounts", response_model=PoolAccountResponse, status_code=201)
async def add_pool_account(
    request: PoolAccountCreateRequest,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> PoolAccountResponse:
    """Add a new account to the Bedrock pool.

    Requires platform admin privileges.
    """
    await access.check_permission(current_user, Permission.POOL_MANAGE)
    return await service.add_pool_account(request)


@router.delete("/pool/accounts/{account_id}", status_code=204)
async def remove_pool_account(
    account_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> None:
    """Remove an account from the Bedrock pool.

    Requires platform admin privileges.
    """
    await access.check_permission(current_user, Permission.POOL_MANAGE)
    await service.remove_pool_account(account_id)


# Log Viewer Endpoints


@router.get("/logs", response_model=LogQueryResponse)
async def query_logs(
    log_service: Annotated[LogService, Depends(get_log_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    org_id: str | None = None,
    user_id: str | None = None,
    status_code: int | None = None,
    path_pattern: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> LogQueryResponse:
    """Query request logs.

    Platform admins can query all logs.
    Org admins can only query logs for their organization.
    """
    await access.check_permission(current_user, Permission.LOGS_READ, target_org_id=org_id)

    # Restrict org_id based on access level
    accessible_orgs = await access.get_accessible_organizations(current_user)
    if accessible_orgs is not None and org_id:
        if org_id not in accessible_orgs:
            from src.admin.exceptions import AccessDeniedError

            raise AccessDeniedError("Cannot access logs from other organizations")
    elif accessible_orgs is not None and len(accessible_orgs) > 0:
        org_id = accessible_orgs[0]  # Default to user's org

    filters: dict[str, Any] = {}
    if start_time:
        filters["start_time"] = start_time
    if end_time:
        filters["end_time"] = end_time
    if org_id:
        filters["org_id"] = org_id
    if user_id:
        filters["user_id"] = user_id
    if status_code:
        filters["status_code"] = status_code
    if path_pattern:
        filters["path_pattern"] = path_pattern

    try:
        logs, total = await log_service.query_logs(filters, page, page_size)
    except Exception:
        # Table may not exist yet or query failed — return empty results
        import logging

        logging.getLogger("bedrockgateway").warning("Log query failed (table may not exist yet)")
        logs, total = [], 0

    return LogQueryResponse(
        items=logs,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < total,
    )


# Dashboard Endpoints


@router.get("/dashboard/platform", response_model=PlatformDashboardResponse)
async def get_platform_dashboard(
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> PlatformDashboardResponse:
    """Get platform admin dashboard data.

    Requires platform admin privileges.
    """
    access.require_platform_admin(current_user)

    # Get pool status — may fail if table doesn't exist yet
    try:
        pool_status = await service.get_pool_status()
    except Exception:
        pool_status = PoolStatusResponse(
            total_accounts=0,
            healthy_accounts=0,
            unhealthy_accounts=0,
            accounts=[],
        )

    # Issue #1003: Wire dashboard tiles to usage_logs aggregates
    try:
        metrics = await service.get_platform_metrics_24h()
        top_orgs = await service.get_top_organizations_24h(limit=5)
    except Exception:
        metrics = {
            "total_organizations": 0,
            "total_requests_24h": 0,
            "total_tokens_24h": 0,
            "total_cost_24h": 0,
            "active_users_24h": 0,
            "error_rate_24h": 0.0,
        }
        top_orgs = []

    return PlatformDashboardResponse(
        total_organizations=metrics["total_organizations"],
        total_requests_24h=metrics["total_requests_24h"],
        total_tokens_24h=metrics["total_tokens_24h"],
        total_cost_24h=metrics["total_cost_24h"],
        active_users_24h=metrics["active_users_24h"],
        error_rate_24h=metrics["error_rate_24h"],
        pool_status=pool_status,
        top_organizations=top_orgs,
    )


@router.get("/dashboard/org/{org_id}", response_model=OrgDashboardResponse)
async def get_org_dashboard(
    org_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> OrgDashboardResponse:
    """Get org admin dashboard data."""
    await access.check_permission(current_user, Permission.USAGE_READ, target_org_id=org_id)

    # Get org details — may fail if table doesn't exist yet
    try:
        org = await service.get_organization(org_id)
        org_name = org.name
    except Exception:
        org_name = org_id

    # Issue #1003: Wire dashboard tiles to usage_logs aggregates
    try:
        metrics = await service.get_org_metrics_24h(org_id)
        top_departments = await service.get_top_departments_24h(org_id, limit=5)
        top_models = await service.get_top_models_24h(org_id, limit=5)
    except Exception:
        metrics = {
            "total_requests_24h": 0,
            "total_tokens_24h": 0,
            "total_cost_24h": 0,
            "active_users_24h": 0,
            "error_rate_24h": 0.0,
        }
        top_departments = []
        top_models = []

    return OrgDashboardResponse(
        org_id=org_id,
        org_name=org_name,
        total_requests_24h=metrics["total_requests_24h"],
        total_tokens_24h=metrics["total_tokens_24h"],
        total_cost_24h=metrics["total_cost_24h"],
        active_users_24h=metrics["active_users_24h"],
        error_rate_24h=metrics["error_rate_24h"],
        budget_status={},
        top_departments=top_departments,
        top_models=top_models,
    )


# Department Endpoints


@router.post("/organizations/{org_id}/departments", response_model=DepartmentResponse, status_code=201)
async def create_department(
    org_id: str,
    request: DepartmentCreateRequest,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    cognito_service: Annotated[CognitoService | None, Depends(get_cognito_service)] = None,
) -> DepartmentResponse:
    """Create a new department within an organization.

    Requires org admin privileges.
    """
    await access.check_permission(current_user, Permission.ORG_UPDATE, target_org_id=org_id)
    return await service.create_department(org_id, request, cognito_service)


@router.get("/organizations/{org_id}/departments", response_model=DepartmentListResponse)
async def list_departments(
    org_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> DepartmentListResponse:
    """List departments in an organization."""
    await access.check_permission(current_user, Permission.ORG_READ, target_org_id=org_id)

    depts, total = await service.list_departments(org_id, page, page_size)

    return DepartmentListResponse(
        items=depts,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < total,
    )


@router.get("/organizations/{org_id}/departments/{dept_id}", response_model=DepartmentResponse)
async def get_department(
    org_id: str,
    dept_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> DepartmentResponse:
    """Get department details."""
    await access.check_permission(current_user, Permission.ORG_READ, target_org_id=org_id)
    return await service.get_department(org_id, dept_id)


@router.put("/organizations/{org_id}/departments/{dept_id}", response_model=DepartmentResponse)
async def update_department(
    org_id: str,
    dept_id: str,
    request: DepartmentUpdateRequest,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> DepartmentResponse:
    """Update department details."""
    await access.check_permission(current_user, Permission.ORG_UPDATE, target_org_id=org_id)
    return await service.update_department(org_id, dept_id, request)


@router.delete("/organizations/{org_id}/departments/{dept_id}", status_code=204)
async def delete_department(
    org_id: str,
    dept_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    cognito_service: Annotated[CognitoService | None, Depends(get_cognito_service)] = None,
) -> None:
    """Delete a department."""
    await access.check_permission(current_user, Permission.ORG_UPDATE, target_org_id=org_id)
    await service.delete_department(org_id, dept_id, cognito_service)


# Team Endpoints


@router.post("/organizations/{org_id}/departments/{dept_id}/teams", response_model=TeamResponse, status_code=201)
async def create_team(
    org_id: str,
    dept_id: str,
    request: TeamCreateRequest,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> TeamResponse:
    """Create a new team within a department.

    Requires org admin privileges.
    """
    await access.check_permission(current_user, Permission.ORG_UPDATE, target_org_id=org_id)
    return await service.create_team(org_id, dept_id, request)


@router.get("/organizations/{org_id}/departments/{dept_id}/teams", response_model=TeamListResponse)
async def list_teams(
    org_id: str,
    dept_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> TeamListResponse:
    """List teams in a department."""
    await access.check_permission(current_user, Permission.ORG_READ, target_org_id=org_id)

    teams, total = await service.list_teams(org_id, dept_id, page, page_size)

    return TeamListResponse(
        items=teams,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < total,
    )


@router.put("/organizations/{org_id}/teams/{team_id}", response_model=TeamResponse)
async def update_team(
    org_id: str,
    team_id: str,
    request: TeamUpdateRequest,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> TeamResponse:
    """Update team details."""
    await access.check_permission(current_user, Permission.ORG_UPDATE, target_org_id=org_id)
    return await service.update_team(org_id, team_id, request)


@router.delete("/organizations/{org_id}/teams/{team_id}", status_code=204)
async def delete_team(
    org_id: str,
    team_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> None:
    """Delete a team."""
    await access.check_permission(current_user, Permission.ORG_UPDATE, target_org_id=org_id)
    await service.delete_team(org_id, team_id)


# User Management Endpoints


@router.post("/organizations/{org_id}/teams/{team_id}/users", response_model=UserResponse, status_code=201)
async def add_user(
    org_id: str,
    team_id: str,
    request: UserCreateRequest,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    cognito_service: Annotated[CognitoService | None, Depends(get_cognito_service)] = None,
) -> UserResponse:
    """Add a new user to a team.

    Creates user in Cognito (if configured) and database.
    Requires org admin privileges.
    """
    await access.check_permission(current_user, Permission.ORG_UPDATE, target_org_id=org_id)
    # Guard the free-form role field: a caller may only grant a role at or below
    # their own privilege. Without this an org_admin could create a user with
    # role="platform_admin" and escalate out of their own organization.
    await access.require_assignable_role(current_user, request.role, target_org_id=org_id)
    return await service.add_user(org_id, team_id, request, cognito_service)


@router.get("/organizations/{org_id}/users", response_model=UserListResponse)
async def list_users_org(
    org_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> UserListResponse:
    """List all users in an organization."""
    await access.check_permission(current_user, Permission.ORG_READ, target_org_id=org_id)

    users, total = await service.list_users_org(org_id, page, page_size)

    return UserListResponse(
        items=users,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < total,
    )


@router.get("/organizations/{org_id}/teams/{team_id}/users", response_model=UserListResponse)
async def list_users_team(
    org_id: str,
    team_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> UserListResponse:
    """List users in a team."""
    await access.check_permission(current_user, Permission.ORG_READ, target_org_id=org_id)

    users, total = await service.list_users_team(org_id, team_id, page, page_size)

    return UserListResponse(
        items=users,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < total,
    )


@router.delete("/organizations/{org_id}/users/{user_id}", status_code=204)
async def remove_user(
    org_id: str,
    user_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    cognito_service: Annotated[CognitoService | None, Depends(get_cognito_service)] = None,
) -> None:
    """Remove a user from the organization.

    Deletes user from Cognito (if configured) and database.
    Requires org admin privileges.
    """
    await access.check_permission(current_user, Permission.ORG_UPDATE, target_org_id=org_id)
    await service.remove_user(org_id, user_id, cognito_service)


# Service Account Endpoints


@router.post("/organizations/{org_id}/service-accounts", response_model=ServiceAccountResponse, status_code=201)
async def create_service_account(
    org_id: str,
    request: ServiceAccountCreateRequest,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> ServiceAccountResponse:
    """Create a new service account.

    Requires org admin privileges.
    Note: department_id and team_id are extracted from the request or default to org-level.
    """
    await access.check_permission(current_user, Permission.ORG_UPDATE, target_org_id=org_id)
    # For org-level service accounts, we'll use placeholder values
    return await service.create_service_account(org_id, "default", "default", request)


@router.get("/organizations/{org_id}/service-accounts", response_model=ServiceAccountListResponse)
async def list_service_accounts(
    org_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ServiceAccountListResponse:
    """List service accounts in an organization."""
    await access.check_permission(current_user, Permission.ORG_READ, target_org_id=org_id)

    sas, total = await service.list_service_accounts(org_id, page, page_size)

    return ServiceAccountListResponse(
        items=sas,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < total,
    )


@router.delete("/organizations/{org_id}/service-accounts/{sa_id}", status_code=204)
async def delete_service_account(
    org_id: str,
    sa_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> None:
    """Delete a service account.

    Requires org admin privileges.
    """
    await access.check_permission(current_user, Permission.ORG_UPDATE, target_org_id=org_id)
    await service.delete_service_account(org_id, sa_id)


# =============================================================================
# Agent Management Endpoints (Issue #119)
# =============================================================================


def get_agent_service() -> AgentService:
    """Get agent service instance."""
    return AgentService()


@router.post("/agents", response_model=AgentResponse, status_code=201)
async def create_agent(
    request: AgentCreateRequest,
    service: Annotated[AgentService, Depends(get_agent_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> AgentResponse:
    """Create a new agent (Cognito App Client).

    Issue #119: Creates a Cognito App Client for M2M authentication.
    The agent can then use client_credentials flow to obtain access tokens.

    Requires org admin privileges.
    """
    await access.check_permission(current_user, Permission.ORG_UPDATE, target_org_id=request.org_id)
    return await service.create_agent(request)


@router.get("/agents", response_model=AgentListResponse)
async def list_agents(
    service: Annotated[AgentService, Depends(get_agent_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    org_id: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AgentListResponse:
    """List agents for an organization.

    Issue #119: Returns all Cognito App Clients (agents) for the organization.

    Platform admins can specify org_id to list agents for any org.
    Org admins can only list agents for their own organization.
    """
    # Determine which org to query
    if org_id:
        await access.check_permission(current_user, Permission.ORG_READ, target_org_id=org_id)
    else:
        org_id = current_user.org_id

    return await service.list_agents(org_id, page, page_size)


@router.get("/agents/{client_id}", response_model=AgentResponse)
async def get_agent(
    client_id: str,
    service: Annotated[AgentService, Depends(get_agent_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> AgentResponse:
    """Get agent details by client ID.

    Issue #119: Returns details of a specific Cognito App Client.
    """
    # Get agent first to check org
    agent = await service.get_agent(client_id, current_user.org_id)
    await access.check_permission(current_user, Permission.ORG_READ, target_org_id=agent.org_id)
    return agent


@router.get("/agents/{client_id}/credentials", response_model=AgentCredentialsResponse)
async def get_agent_credentials(
    client_id: str,
    service: Annotated[AgentService, Depends(get_agent_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> AgentCredentialsResponse:
    """Get agent credentials (client_id and client_secret).

    Issue #119: Returns the client_secret for a Cognito App Client.
    This is needed for the client_credentials flow.

    WARNING: The client_secret should be stored securely. This is typically
    a one-time retrieval operation.

    Requires org admin privileges.
    """
    # Get agent first to check org
    agent = await service.get_agent(client_id, current_user.org_id)
    await access.check_permission(current_user, Permission.ORG_UPDATE, target_org_id=agent.org_id)
    return await service.get_agent_credentials(client_id, current_user.org_id)


@router.put("/agents/{client_id}", response_model=AgentResponse)
async def update_agent(
    client_id: str,
    request: AgentUpdateRequest,
    service: Annotated[AgentService, Depends(get_agent_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> AgentResponse:
    """Update agent metadata.

    Issue #119: Updates the metadata for a Cognito App Client.
    This updates the DynamoDB record used by the Pre Token Generation Lambda.

    Requires org admin privileges.
    """
    # Get agent first to check org
    agent = await service.get_agent(client_id, current_user.org_id)
    await access.check_permission(current_user, Permission.ORG_UPDATE, target_org_id=agent.org_id)
    return await service.update_agent(client_id, current_user.org_id, request)


@router.delete("/agents/{client_id}", status_code=204)
async def delete_agent(
    client_id: str,
    service: Annotated[AgentService, Depends(get_agent_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> None:
    """Delete an agent.

    Issue #119: Deletes the Cognito App Client and associated metadata.
    This immediately revokes the agent's ability to obtain new tokens.

    Requires org admin privileges.
    """
    # Get agent first to check org
    agent = await service.get_agent(client_id, current_user.org_id)
    await access.check_permission(current_user, Permission.ORG_UPDATE, target_org_id=agent.org_id)
    await service.delete_agent(client_id, current_user.org_id)


# =============================================================================
# User Roles Endpoint (Issue #179)
# =============================================================================


@router.get("/users/roles")
async def get_available_roles(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> dict[str, list[str]]:
    """Get list of available user roles.

    Issue #179: Returns static list of available roles for the admin UI.
    This is a simple endpoint that doesn't require database access.

    Any authenticated user can access this endpoint.
    """
    from src.admin.schemas import AvailableRolesResponse

    return AvailableRolesResponse(roles=["platform_admin", "org_admin", "user", "service_account"]).model_dump()


# =============================================================================
# Usage Timeseries Endpoint (Issue #179)
# =============================================================================


@router.get("/organizations/{org_id}/usage/timeseries", response_model=UsageTimeseriesResponse)
async def get_usage_timeseries(
    org_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    period: Annotated[str, Query(description="Aggregation period: daily, weekly, monthly")] = "daily",
    start: Annotated[str | None, Query(description="Start date (YYYY-MM-DD)")] = None,
    end: Annotated[str | None, Query(description="End date (YYYY-MM-DD)")] = None,
) -> UsageTimeseriesResponse:
    """Get usage data over time for charts.

    Issue #179: Returns time-series usage data for the organization dashboard.
    Aggregates usage_logs by date and returns token counts, costs, and request counts.

    Requires usage:read permission for the organization.
    """
    from src.admin.schemas import UsageTimeseriesDataPoint

    await access.check_permission(current_user, Permission.USAGE_READ, target_org_id=org_id)

    data_points = await service.get_usage_timeseries(
        org_id=org_id,
        period=period,
        start_date=start,
        end_date=end,
    )

    # Convert to response schema
    return UsageTimeseriesResponse(
        data=[UsageTimeseriesDataPoint(**dp) for dp in data_points],
        period=period,
        org_id=org_id,
    )


# =============================================================================
# My Chats Endpoints (Issue #179)
# =============================================================================


@router.get("/users/me/chats", response_model=ChatListResponse)
async def get_my_chats(
    service: Annotated[AdminService, Depends(get_admin_service)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    limit: Annotated[int, Query(ge=1, le=100, description="Items per page")] = 20,
    model: Annotated[str | None, Query(description="Filter by model name")] = None,
    start_date: Annotated[str | None, Query(description="Start date (YYYY-MM-DD)")] = None,
    end_date: Annotated[str | None, Query(description="End date (YYYY-MM-DD)")] = None,
) -> ChatListResponse:
    """Get current user's chat history.

    Issue #179: Returns paginated list of user's chats through the gateway.
    The user identity is extracted from the JWT token.

    Chat content is available if the chat logging feature (issue #143) is enabled.
    Otherwise, only usage metadata (tokens, cost) is returned.
    """
    from src.admin.schemas import ChatSummary

    chats, total = await service.get_user_chats(
        user_id=current_user.user_id,
        org_id=current_user.org_id,
        page=page,
        limit=limit,
        model_filter=model,
        start_date=start_date,
        end_date=end_date,
    )

    return ChatListResponse(
        chats=[ChatSummary(**chat) for chat in chats],
        total=total,
        page=page,
        limit=limit,
    )


@router.get("/users/me/chats/{request_id}", response_model=ChatDetailResponse)
async def get_my_chat_detail(
    request_id: str,
    service: Annotated[AdminService, Depends(get_admin_service)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> ChatDetailResponse:
    """Get details of a specific chat.

    Issue #179: Returns full details of a chat including the conversation
    if chat logging (issue #143) is enabled.

    The user can only access their own chats (verified via JWT user_id).
    """
    from src.admin.exceptions import ResourceNotFoundError

    chat_detail = await service.get_chat_detail(
        user_id=current_user.user_id,
        org_id=current_user.org_id,
        request_id=request_id,
    )

    if not chat_detail:
        raise ResourceNotFoundError("Chat", request_id)

    return ChatDetailResponse(**chat_detail)


# =============================================================================
# Cognito-backed Entity List Endpoints (Issue #226)
# =============================================================================


def _cognito_user_to_response(user: dict) -> CognitoUserResponse:
    """Convert Cognito user dict to response schema.

    Issue #226: Helper function to transform Cognito user data.
    """
    # Extract attributes from Cognito user dict
    attrs = {attr["Name"]: attr["Value"] for attr in user.get("Attributes", [])}

    return CognitoUserResponse(
        username=user.get("Username", ""),
        email=attrs.get("email"),
        name=attrs.get("name"),
        github_username=attrs.get("custom:github_username"),
        org_id=attrs.get("custom:org_id"),
        department_id=attrs.get("custom:department_id"),
        team_id=attrs.get("custom:team_id"),
        role=attrs.get("custom:role"),
        status=user.get("UserStatus"),
        enabled=user.get("Enabled", True),
        created_at=user.get("UserCreateDate"),
        updated_at=user.get("UserLastModifiedDate"),
    )


def _cognito_group_to_team(group: dict) -> CognitoTeamResponse:
    """Convert Cognito group dict to team response schema.

    Issue #226: Helper function to transform Cognito group data to team.
    """
    return CognitoTeamResponse(
        group_name=group.get("GroupName", ""),
        description=group.get("Description"),
        created_at=group.get("CreationDate"),
        updated_at=group.get("LastModifiedDate"),
    )


@router.get("/organizations/{org_id}/cognito/users", response_model=CognitoUserListResponse)
async def list_cognito_users(
    org_id: str,
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    cognito_service: Annotated[CognitoService | None, Depends(get_cognito_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> CognitoUserListResponse:
    """List users from Cognito for an organization.

    Issue #226: Cognito as single source of truth for users.
    This endpoint reads user data directly from Cognito, filtered by
    the custom:org_id attribute.

    Note: This is a read-only endpoint. User creation/modification
    should be done via Cognito console or API directly.

    Requires org:read permission for the organization.
    """
    from src.admin.exceptions import CognitoNotConfiguredError

    await access.check_permission(current_user, Permission.ORG_READ, target_org_id=org_id)

    if not cognito_service:
        raise CognitoNotConfiguredError()

    users, total = cognito_service.list_users_by_org(org_id, page, page_size)

    items = [_cognito_user_to_response(user) for user in users]

    return CognitoUserListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < total,
    )


@router.get("/organizations/{org_id}/cognito/teams", response_model=CognitoTeamListResponse)
async def list_cognito_teams(
    org_id: str,
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    cognito_service: Annotated[CognitoService | None, Depends(get_cognito_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> CognitoTeamListResponse:
    """List teams for an organization.

    Issue #226: Teams are derived from the custom:team_id attribute on users
    in Cognito. This endpoint extracts unique team IDs from all users in the
    specified organization.

    If Cognito groups exist for the org, those are returned as well.
    Otherwise, teams are derived purely from user attributes.

    Requires org:read permission for the organization.
    """
    from src.admin.exceptions import CognitoNotConfiguredError

    await access.check_permission(current_user, Permission.ORG_READ, target_org_id=org_id)

    if not cognito_service:
        raise CognitoNotConfiguredError()

    # Primary: derive teams from custom:team_id user attributes (same approach as departments)
    team_ids = cognito_service.get_unique_teams(org_id)

    # Apply pagination
    total = len(team_ids)
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    paginated = team_ids[start_idx:end_idx]

    items = [CognitoTeamResponse(group_name=team_id, description=None, created_at=None, updated_at=None) for team_id in paginated]

    return CognitoTeamListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < total,
    )


@router.get("/organizations/{org_id}/cognito/departments", response_model=CognitoDepartmentListResponse)
async def list_cognito_departments(
    org_id: str,
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    cognito_service: Annotated[CognitoService | None, Depends(get_cognito_service)],
) -> CognitoDepartmentListResponse:
    """List unique departments from Cognito users in an organization.

    Issue #226: Departments are derived from the custom:department_id attribute
    on users in Cognito. This endpoint extracts unique department IDs from
    all users in the specified organization.

    Note: This is a read-only endpoint. Department values come from
    user attributes set in Cognito.

    Requires org:read permission for the organization.
    """
    from src.admin.exceptions import CognitoNotConfiguredError

    await access.check_permission(current_user, Permission.ORG_READ, target_org_id=org_id)

    if not cognito_service:
        raise CognitoNotConfiguredError()

    department_ids = cognito_service.get_unique_departments(org_id)

    items = [CognitoDepartmentResponse(department_id=dept_id) for dept_id in department_ids]

    return CognitoDepartmentListResponse(
        items=items,
        total=len(items),
    )


# =============================================================================
# Agent Registry Endpoints (Issue #248)
# =============================================================================
# These endpoints manage agents in the DynamoDB agent registry for IAM/SigV4
# authentication. This is separate from the Cognito-based agent management
# (Issue #119) which uses client_credentials flow.


def get_agent_registry_service() -> AgentRegistryService:
    """Get agent registry service instance."""
    return AgentRegistryService()


@router.post(
    "/registry/agents",
    response_model=AgentRegistryResponse,
    status_code=201,
    tags=["agent-registry"],
)
async def create_registry_agent(
    request: AgentRegistryCreateRequest,
    service: Annotated[AgentRegistryService, Depends(get_agent_registry_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> AgentRegistryResponse:
    """Register a new agent in the DynamoDB registry.

    Issue #248: Creates an agent entry for IAM/SigV4 authentication.
    The agent_id is a UUID generated server-side.
    The role_arn must be unique across all agents.

    Requires the AGENT_REGISTER permission for the target organization.

    Issue #3989 (f-2c3ccdce): previously gated on ORG_UPDATE, which every role
    able to edit any org attribute holds. Registry writes mint authenticated
    identity, so they get their own permission.
    """
    await access.check_permission(current_user, Permission.AGENT_REGISTER, target_org_id=request.org_id)
    logger.info(
        "agent_registry_create_allowed caller=%s target_org=%s role_arn=%s",
        current_user.user_id,
        request.org_id,
        request.role_arn,
    )
    return await service.create_agent(request)


@router.get(
    "/registry/agents",
    response_model=AgentRegistryListResponse,
    tags=["agent-registry"],
)
async def list_registry_agents(
    service: Annotated[AgentRegistryService, Depends(get_agent_registry_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    org_id: Annotated[str | None, Query(description="Filter by organization ID")] = None,
    team_id: Annotated[str | None, Query(description="Filter by team ID (requires org_id)")] = None,
    owner: Annotated[str | None, Query(description="Filter by owner")] = None,
    page_size: Annotated[int, Query(ge=1, le=100, description="Items per page")] = 50,
    last_key: Annotated[str | None, Query(description="Pagination token from previous response")] = None,
) -> AgentRegistryListResponse:
    """List agents from the DynamoDB registry.

    Issue #248: Returns agents with optional filtering by org_id, team_id, or owner.
    Uses DynamoDB GSIs for efficient querying.

    Platform admins can list all agents or filter by any org.
    Org admins can only list agents for their own organization.
    """
    # Determine which org to query
    is_platform_admin = False
    if org_id:
        await access.check_permission(current_user, Permission.ORG_READ, target_org_id=org_id)
    else:
        # Issue #3988: is_platform_admin is async — the missing `await` made this
        # branch dead code (a coroutine is always truthy), so a non-platform
        # caller fell through with org_id=None into an unfiltered cross-tenant scan.
        is_platform_admin = await access.is_platform_admin(current_user)
        if not is_platform_admin:
            # Non-platform admins are pinned to their own org. TokenContext.org_id
            # is populated as `claims.org_id or ""`, so an empty string must be
            # rejected rather than left falsy — otherwise it reaches _scan_all.
            if not current_user.org_id:
                from src.admin.exceptions import AccessDeniedError

                raise AccessDeniedError(
                    message="No organization membership — cannot list registered agents",
                    required_permission=Permission.ORG_READ.value,
                )
            org_id = current_user.org_id

    return await service.list_agents(
        org_id=org_id,
        team_id=team_id,
        owner=owner,
        page_size=page_size,
        last_key=last_key,
        # Only a verified platform admin may reach the cross-tenant scan.
        allow_scan=is_platform_admin,
    )


@router.get(
    "/registry/agents/{agent_id}",
    response_model=AgentRegistryResponse,
    tags=["agent-registry"],
)
async def get_registry_agent(
    agent_id: str,
    service: Annotated[AgentRegistryService, Depends(get_agent_registry_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> AgentRegistryResponse:
    """Get agent details from the DynamoDB registry.

    Issue #248: Returns agent details by agent_id (UUID).
    """
    agent = await service.get_agent(agent_id)
    await access.check_permission(current_user, Permission.ORG_READ, target_org_id=agent.org_id)
    return agent


@router.patch(
    "/registry/agents/{agent_id}",
    response_model=AgentRegistryResponse,
    tags=["agent-registry"],
)
async def update_registry_agent(
    agent_id: str,
    request: AgentRegistryUpdateRequest,
    service: Annotated[AgentRegistryService, Depends(get_agent_registry_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> AgentRegistryResponse:
    """Update agent configuration in the DynamoDB registry.

    Issue #248: Updates agent attributes. The role_arn can be updated
    if the IAM role changes, but must remain unique.

    Requires the AGENT_REGISTER permission for the agent's organization
    (Issue #3989: repointing role_arn mints identity exactly as a create does).
    """
    agent = await service.get_agent(agent_id)
    await access.check_permission(current_user, Permission.AGENT_REGISTER, target_org_id=agent.org_id)
    logger.info(
        "agent_registry_update_allowed caller=%s target_org=%s agent_id=%s new_role_arn=%s",
        current_user.user_id,
        agent.org_id,
        agent_id,
        request.role_arn,
    )
    return await service.update_agent(agent_id, request)


@router.delete(
    "/registry/agents/{agent_id}",
    status_code=204,
    tags=["agent-registry"],
)
async def delete_registry_agent(
    agent_id: str,
    service: Annotated[AgentRegistryService, Depends(get_agent_registry_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> None:
    """Disable an agent in the DynamoDB registry (soft delete).

    Issue #248: Sets the agent status to 'disabled'. The Lambda
    authorizer will deny requests from disabled agents.

    Requires org admin privileges for the agent's organization.
    """
    agent = await service.get_agent(agent_id)
    await access.check_permission(current_user, Permission.ORG_UPDATE, target_org_id=agent.org_id)
    await service.delete_agent(agent_id)


# =============================================================================
# Agent Usage Endpoint (Issue #249)
# =============================================================================


@router.get(
    "/registry/agents/{agent_id}/usage",
    response_model=AgentUsageResponse,
    tags=["agent-registry"],
)
async def get_registry_agent_usage(
    agent_id: str,
    service: Annotated[AgentRegistryService, Depends(get_agent_registry_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    period: Annotated[str, Query(description="Aggregation period: daily, weekly, monthly")] = "monthly",
    start: Annotated[str | None, Query(description="Start date (YYYY-MM-DD)")] = None,
    end: Annotated[str | None, Query(description="End date (YYYY-MM-DD)")] = None,
) -> AgentUsageResponse:
    """Get usage data for an agent.

    Issue #249: Per-Agent Budget Assignment and Usage Dashboard

    Returns usage statistics including request counts, token counts, costs,
    and budget status if the agent has a budget configured.

    Query Parameters:
        period: Aggregation period (daily/weekly/monthly), default "monthly"
        start: Start date filter (YYYY-MM-DD)
        end: End date filter (YYYY-MM-DD)

    Response includes:
        - Total requests, input tokens, output tokens, cost
        - Breakdown by model (if available)
        - Budget status (limit, used, remaining, utilization %)

    Requires org read permission for the agent's organization.
    """
    agent = await service.get_agent(agent_id)
    await access.check_permission(current_user, Permission.USAGE_READ, target_org_id=agent.org_id)
    return await service.get_agent_usage(agent_id, period=period, start_date=start, end_date=end)


# =============================================================================
# =============================================================================
# Agent Onboarding Endpoint (Issue #250)
# =============================================================================


def get_agent_onboarding_service() -> AgentOnboardingService:
    """Get agent onboarding service instance."""
    return AgentOnboardingService()


@router.post(
    "/agents/onboard",
    response_model=AgentOnboardResponse,
    status_code=201,
    tags=["agent-onboarding"],
)
async def onboard_agent(
    request: AgentOnboardRequest,
    service: Annotated[AgentOnboardingService, Depends(get_agent_onboarding_service)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentOnboardResponse:
    """Onboard a new agent to the platform (Bring Your Own IAM Role).

    Issue #250: Epic 11 - US 3 - Unit 1 - Agent Onboarding Orchestrator

    This endpoint orchestrates the full agent onboarding flow:
    1. Validates the IAM role exists (for same-account roles)
    2. Creates a K8s ServiceAccount with IRSA annotation
    3. Creates budget config in Postgres (if budget_monthly_usd provided)
    4. Registers the agent in DynamoDB

    The developer provides their own IAM role ARN (Bring Your Own Role).
    The platform does NOT create or modify IAM roles.

    Response includes an IRSA trust policy snippet that the developer
    needs to add to their IAM role's trust policy.

    Requires the AGENT_REGISTER permission for the target organization.

    Issue #3989 (f-2c3ccdce): this endpoint previously had NO authorization check
    at all ("self-service onboarding"), while ``request.org_id`` is a required,
    entirely caller-supplied field. A registry row resolves its ``role_arn`` to an
    authenticated ``service`` identity in its org
    (``src/auth/agent_registry.py``), so an ungated write here let any
    authenticated caller grant an arbitrary IAM role platform identity inside an
    org of their choosing.
    """
    # Gate on AGENT_REGISTER scoped to the target org. Deliberately NOT ORG_UPDATE:
    # that permission is held by every role able to edit any org attribute, and
    # minting an authenticated identity is a strictly higher-privilege operation.
    try:
        await access.check_permission(current_user, Permission.AGENT_REGISTER, target_org_id=request.org_id)
    except Exception:
        # Audit trail: this endpoint mints identity, so denials are logged too.
        logger.warning(
            "agent_onboard_denied caller=%s caller_org=%s target_org=%s role_arn=%s",
            current_user.user_id,
            current_user.org_id,
            request.org_id,
            request.role_arn,
        )
        raise

    # The permission check scopes org_id, but team_id/owner/level are separately
    # caller-controlled: without this, a caller authorized for org X could still
    # register into an arbitrary team and claim arbitrary ownership within it.
    await _validate_onboard_target_scope(request, current_user, access, db)

    logger.info(
        "agent_onboard_allowed caller=%s target_org=%s team=%s level=%s role_arn=%s",
        current_user.user_id,
        request.org_id,
        request.team_id,
        request.level,
        request.role_arn,
    )
    return await service.onboard_agent(request)


async def _validate_onboard_target_scope(
    request: AgentOnboardRequest,
    current_user: TokenContext,
    access: AccessControl,
    db: AsyncSession,
) -> None:
    """Validate the caller-supplied team/owner/level on an onboard request.

    Issue #3989: ``check_permission(..., target_org_id=request.org_id)`` gates the
    org, but ``team_id``, ``owner`` and ``level`` are separate free-form fields on
    the request. Each is validated against the *target org* so a caller cannot
    register an agent into a team that belongs to some other tenant, attribute
    ownership to a user outside the target org, or claim org-wide reach without
    org-admin authority.

    Raises:
        InvalidScopeError: team_id or owner does not belong to request.org_id.
        AccessDeniedError: level="org" requested by a non-org-admin caller.
    """
    from src.admin.exceptions import AccessDeniedError, InvalidScopeError
    from src.shared.models.organization import Team, User

    if request.team_id:
        team = (await db.execute(select(Team.id).where(Team.id == request.team_id, Team.org_id == request.org_id).limit(1))).scalar_one_or_none()
        if not team:
            raise InvalidScopeError(
                message="team_id does not belong to the target organization",
                allowed_scope=f"org:{request.org_id}",
                requested_scope=f"team:{request.team_id}",
            )

    # `owner` is a user identifier recorded on the registry row and surfaced by
    # the by-owner GSI. Accept the Cognito sub or the users.id, but require the
    # row to live in the target org.
    owner_in_org = (
        await db.execute(
            select(User.id)
            .where(
                or_(User.id == request.owner, User.cognito_sub == request.owner),
                User.org_id == request.org_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if not owner_in_org:
        raise InvalidScopeError(
            message="owner is not a member of the target organization",
            allowed_scope=f"org:{request.org_id}",
            requested_scope=f"owner:{request.owner}",
        )

    # level="org" gives the agent org-wide reach; only an org admin (or platform
    # admin) may grant it. team/personal stay available to any AGENT_REGISTER holder.
    if request.level == "org" and not await access.is_org_admin(current_user, request.org_id):
        raise AccessDeniedError(
            message="Only an organization administrator may onboard an org-level agent",
            required_permission=Permission.AGENT_REGISTER.value,
        )


# =============================================================================
# Policy Scoping Endpoints (Issue #255)
# =============================================================================


@router.post(
    "/policies/preview",
    response_model=PolicyPreviewResponse,
    tags=["policy-scoping"],
)
async def preview_policies(
    request: PolicyPreviewRequest,
    access: Annotated[AccessControl, Depends(get_access_control)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> PolicyPreviewResponse:
    """Preview IAM policies that would be generated for an agent.

    Issue #255: Policy Scoping Service - IAM Policy & Permission Boundary Generator

    This endpoint allows developers to see what IAM policies would be generated
    for an agent with the given attributes before actually onboarding the agent.
    Useful for understanding permissions and debugging policy issues.

    The response includes:
    - Resource prefix generated from hierarchy
    - Permission boundary (hard ceiling based on hierarchy level)
    - Agent IAM policy (actual permissions based on agent_type)
    - Trust policy (IRSA, only if OIDC config provided)
    - Required tags for resources created by the agent
    - Tag enforcement statements to combine with agent policy

    Requires platform admin or org admin privileges.
    """
    from src.admin.policy_scoping_service import PolicyScopingService
    from src.shared.exceptions import ValidationError

    # Platform admins can preview any policy
    # Org admins can preview policies for their org hierarchy
    # Issue #3988: is_platform_admin is async — without `await` the coroutine is
    # always truthy, so this whole scope check was dead code.
    if not await access.is_platform_admin(current_user):
        # Check that the org in hierarchy matches the user's org
        hierarchy_org = request.hierarchy.get("org")
        if hierarchy_org and hierarchy_org != current_user.org_id:
            from src.admin.exceptions import AccessDeniedError

            raise AccessDeniedError(
                "Can only preview policies for your own organization hierarchy",
                required_permission="platform_admin or matching org_id",
                user_role=current_user.org_id,
            )

    service = PolicyScopingService()

    try:
        # Generate resource prefix
        resource_prefix = service.generate_resource_prefix(
            level=request.level,
            hierarchy=request.hierarchy,
        )

        # Generate permission boundary
        permission_boundary = service.generate_permission_boundary(
            resource_prefix=resource_prefix,
            region=request.region,
            account_id=request.account_id,
        )

        # Generate agent policy
        agent_policy = service.generate_agent_policy(
            agent_type=request.agent_type,
            resource_prefix=resource_prefix,
            region=request.region,
            account_id=request.account_id,
            api_gateway_arn=request.api_gateway_arn,
        )

        # Generate required tags
        required_tags = service.generate_required_tags(
            level=request.level,
            hierarchy=request.hierarchy,
        )
    except ValueError as e:
        raise ValidationError(str(e)) from e

    # Generate trust policy if OIDC config provided
    trust_policy = None
    if all(
        [
            request.oidc_provider_arn,
            request.oidc_issuer,
            request.namespace,
            request.service_account_name,
        ]
    ):
        trust_policy = service.generate_trust_policy(
            oidc_provider_arn=request.oidc_provider_arn,  # type: ignore[arg-type]
            oidc_issuer=request.oidc_issuer,  # type: ignore[arg-type]
            namespace=request.namespace,  # type: ignore[arg-type]
            service_account_name=request.service_account_name,  # type: ignore[arg-type]
        )

    # Generate tag enforcement statements
    tag_enforcement_statements = service.generate_tag_enforcement_statements(
        hierarchy=request.hierarchy,
    )

    return PolicyPreviewResponse(
        resource_prefix=resource_prefix,
        permission_boundary=permission_boundary,
        agent_policy=agent_policy,
        trust_policy=trust_policy,
        required_tags=required_tags,
        tag_enforcement_statements=tag_enforcement_statements,
    )


@router.get(
    "/policies/agent-types",
    response_model=AgentTypesListResponse,
    tags=["policy-scoping"],
)
async def list_agent_types(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> AgentTypesListResponse:
    """List available agent types and their AWS action permissions.

    Issue #255: Returns all available agent types with their descriptions
    and the AWS actions they are allowed to perform.

    Any authenticated user can access this endpoint.
    """
    from src.admin.policy_scoping_schemas import AgentTypeActionsResponse
    from src.admin.policy_templates import AGENT_TYPE_ACTIONS, AGENT_TYPE_DESCRIPTIONS

    agent_types = [
        AgentTypeActionsResponse(
            agent_type=agent_type,
            actions=actions,
            description=AGENT_TYPE_DESCRIPTIONS.get(agent_type, ""),
        )
        for agent_type, actions in AGENT_TYPE_ACTIONS.items()
    ]

    return AgentTypesListResponse(agent_types=agent_types)
