"""Admin module Pydantic schemas for API request/response validation."""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.admin.config import AdminRole, Permission


# Organization Schemas
class OrganizationCreateRequest(BaseModel):
    """Request schema for creating an organization."""

    name: str = Field(..., min_length=1, max_length=255, description="Organization name")
    aws_accounts: list[str] = Field(default_factory=list, description="List of AWS account IDs")
    role_mappings: dict[str, str] = Field(default_factory=dict, description="Role name to ID mappings")
    settings: dict[str, Any] = Field(default_factory=dict, description="Additional settings")
    # Issue #375: Identity fields for tenant-identity Phase A
    github_installation_ids: list[str] = Field(default_factory=list, description="GitHub App installation IDs")
    cognito_client_ids: list[str] = Field(default_factory=list, description="Cognito App Client IDs")


class OrganizationUpdateRequest(BaseModel):
    """Request schema for updating an organization."""

    name: str | None = Field(None, min_length=1, max_length=255, description="Organization name")
    aws_accounts: list[str] | None = Field(None, description="List of AWS account IDs")
    role_mappings: dict[str, str] | None = Field(None, description="Role name to ID mappings")
    settings: dict[str, Any] | None = Field(None, description="Additional settings")
    # Issue #375: Identity fields for tenant-identity Phase A
    github_installation_ids: list[str] | None = Field(None, description="GitHub App installation IDs")
    cognito_client_ids: list[str] | None = Field(None, description="Cognito App Client IDs")


class OrganizationResponse(BaseModel):
    """Response schema for organization data."""

    id: str
    name: str
    aws_accounts: list[str]
    role_mappings: dict[str, str]
    settings: dict[str, Any]
    # Issue #375: Identity fields for tenant-identity Phase A
    github_installation_ids: list[str] = Field(default_factory=list)
    cognito_client_ids: list[str] = Field(default_factory=list)
    created_at: datetime

    class Config:
        from_attributes = True


class OrganizationListResponse(BaseModel):
    """Response schema for listing organizations."""

    items: list[OrganizationResponse]
    total: int
    page: int
    page_size: int
    has_more: bool


# Pool Schemas
class PoolAccountStatus(str, Enum):
    """Pool account health status."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class PoolAccountResponse(BaseModel):
    """Response schema for pool account data."""

    id: str
    account_id: str
    role_arn: str
    region: str
    is_healthy: bool
    last_health_check: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class PoolStatusResponse(BaseModel):
    """Response schema for pool status."""

    total_accounts: int
    healthy_accounts: int
    unhealthy_accounts: int
    accounts: list[PoolAccountResponse]


class PoolAccountCreateRequest(BaseModel):
    """Request schema for adding a pool account."""

    account_id: str = Field(..., pattern=r"^\d{12}$", description="12-digit AWS account ID")
    role_arn: str = Field(..., pattern=r"^arn:aws:iam::\d{12}:role/.+$", description="IAM role ARN")
    region: str = Field(default="us-east-1", description="AWS region")


# Budget Config Schemas
class BudgetConfigResponse(BaseModel):
    """Response schema for budget configuration."""

    org_id: str
    entity_type: str
    entity_id: str
    period_type: str
    budget_amount_usd: Decimal
    enforcement_mode: str
    updated_at: datetime


class BudgetConfigUpdateRequest(BaseModel):
    """Request schema for updating budget configuration."""

    budget_amount_usd: Decimal | None = Field(None, gt=0, description="Budget amount in USD")
    enforcement_mode: str | None = Field(None, description="Enforcement mode: soft or hard")


# Rate Limit Config Schemas
class RateLimitConfigResponse(BaseModel):
    """Response schema for rate limit configuration."""

    org_id: str
    entity_type: str
    entity_id: str
    rpm: int | None
    tpm: int | None
    concurrent_requests: int | None
    updated_at: datetime


class RateLimitConfigUpdateRequest(BaseModel):
    """Request schema for updating rate limit configuration."""

    rpm: int | None = Field(None, ge=0, description="Requests per minute limit")
    tpm: int | None = Field(None, ge=0, description="Tokens per minute limit")
    concurrent_requests: int | None = Field(None, ge=0, description="Concurrent requests limit")


# Log Schemas
class LogEntryResponse(BaseModel):
    """Response schema for a single log entry."""

    id: str
    timestamp: datetime
    org_id: str
    user_id: str
    method: str
    path: str
    status_code: int
    response_time_ms: int
    request_body_size: int | None
    response_body_size: int | None


class LogQueryRequest(BaseModel):
    """Request schema for querying logs."""

    start_time: datetime | None = None
    end_time: datetime | None = None
    org_id: str | None = None
    user_id: str | None = None
    status_code: int | None = None
    path_pattern: str | None = None
    min_response_time_ms: int | None = None


class LogQueryResponse(BaseModel):
    """Response schema for log query results."""

    items: list[LogEntryResponse]
    total: int
    page: int
    page_size: int
    has_more: bool


# User Role Schemas
class UserRoleResponse(BaseModel):
    """Response schema for user role data."""

    user_id: str
    role: AdminRole
    org_id: str | None
    dept_id: str | None
    permissions: list[Permission]
    created_at: datetime


class UserRoleAssignRequest(BaseModel):
    """Request schema for assigning a user role."""

    user_id: str
    role: AdminRole
    org_id: str | None = None
    dept_id: str | None = None


# Health Check Schemas
class HealthCheckComponent(BaseModel):
    """Health status of a single component."""

    name: str
    status: str
    latency_ms: int | None = None
    error: str | None = None


class HealthCheckResponse(BaseModel):
    """Response schema for health check."""

    status: str
    timestamp: datetime
    components: list[HealthCheckComponent] | None = None


class ReadinessCheckResponse(BaseModel):
    """Response schema for readiness check."""

    status: str
    timestamp: datetime
    components: list[HealthCheckComponent]
    all_healthy: bool


# Dashboard Schemas
class PlatformDashboardResponse(BaseModel):
    """Response schema for platform admin dashboard."""

    total_organizations: int
    total_requests_24h: int
    total_tokens_24h: int
    total_cost_24h: Decimal
    active_users_24h: int
    error_rate_24h: float
    pool_status: PoolStatusResponse
    top_organizations: list[dict[str, Any]]


class OrgDashboardResponse(BaseModel):
    """Response schema for org admin dashboard."""

    org_id: str
    org_name: str
    total_requests_24h: int
    total_tokens_24h: int
    total_cost_24h: Decimal
    active_users_24h: int
    error_rate_24h: float
    budget_status: dict[str, Any]
    top_departments: list[dict[str, Any]]
    top_models: list[dict[str, Any]]


# Budget List Schemas (Issue #185)


class BudgetListItem(BaseModel):
    """Item in budget list response with usage information."""

    entity_type: str
    entity_id: str
    entity_display_name: str | None = Field(None, description="Human-readable name for the entity (e.g. email, github username)")
    period_type: str
    budget_amount_usd: Decimal
    enforcement_mode: str
    current_usage_usd: Decimal = Field(default=Decimal("0.00"))
    utilization_pct: float = Field(default=0.0)
    updated_at: datetime


class BudgetListResponse(BaseModel):
    """Response schema for budget list endpoint."""

    items: list[BudgetListItem]
    total: int
    page: int
    page_size: int
    has_more: bool


class BudgetCreateRequest(BaseModel):
    """Request schema for creating a new budget."""

    entity_type: Literal["org", "department", "team", "user"] = Field(..., description="Entity type: org, department, team, user")
    entity_id: str = Field(..., min_length=1, description="Entity ID")
    period_type: str = Field(..., description="Period type: daily, weekly, monthly")
    budget_amount_usd: Decimal = Field(..., gt=0, description="Budget amount in USD")
    enforcement_mode: str = Field(default="hard", description="Enforcement mode: soft or hard")


class BudgetStatusResponse(BaseModel):
    """Response schema for budget status with current spend."""

    budget_amount_usd: Decimal
    current_spend_usd: Decimal = Field(default=Decimal("0.00"))
    remaining_budget_usd: Decimal = Field(default=Decimal("0.00"))
    budget_utilization_percent: float = Field(default=0.0)
    period_start: str
    period_end: str
    period_type: str
    enforcement_mode: str
    budget_exceeded: bool = False
    warnings: list[str] = Field(default_factory=list)


# Rate Limit List Schemas (Issue #185)


class RateLimitListItem(BaseModel):
    """Item in rate limit list response."""

    entity_type: str
    entity_id: str
    rpm: int | None
    tpm: int | None
    concurrent_requests: int | None
    updated_at: datetime


class RateLimitListResponse(BaseModel):
    """Response schema for rate limit list endpoint."""

    items: list[RateLimitListItem]
    total: int
    page: int
    page_size: int
    has_more: bool


class RateLimitCreateRequest(BaseModel):
    """Request schema for creating a new rate limit."""

    entity_type: Literal["org", "department", "team", "user"] = Field(..., description="Entity type: org, department, team, user")
    entity_id: str = Field(..., min_length=1, description="Entity ID")
    rpm: int | None = Field(None, ge=0, description="Requests per minute limit")
    tpm: int | None = Field(None, ge=0, description="Tokens per minute limit")
    concurrent_requests: int | None = Field(None, ge=0, description="Concurrent requests limit")


# =============================================================================
# User Roles Schemas (Issue #179)
# =============================================================================


class AvailableRolesResponse(BaseModel):
    """Response schema for available user roles.

    Issue #179: Static list of available roles for the admin UI.
    """

    roles: list[str] = Field(
        ...,
        description="List of available user roles",
        examples=[["platform_admin", "org_admin", "user", "service_account"]],
    )


# =============================================================================
# Usage Timeseries Schemas (Issue #179)
# =============================================================================


class UsageTimeseriesDataPoint(BaseModel):
    """A single data point in the usage timeseries."""

    date: str = Field(..., description="Date in YYYY-MM-DD format")
    input_tokens: int = Field(default=0, description="Total input tokens for this period")
    output_tokens: int = Field(default=0, description="Total output tokens for this period")
    cost_usd: Decimal = Field(default=Decimal("0.00"), description="Total cost in USD for this period")
    request_count: int = Field(default=0, description="Total number of requests for this period")


class UsageTimeseriesResponse(BaseModel):
    """Response schema for usage timeseries endpoint.

    Issue #179: Returns usage data aggregated over time for charts.
    """

    data: list[UsageTimeseriesDataPoint] = Field(
        default_factory=list,
        description="List of usage data points",
    )
    period: str = Field(..., description="Aggregation period: daily, weekly, monthly")
    org_id: str = Field(..., description="Organization ID")


# =============================================================================
# My Chats Schemas (Issue #179)
# =============================================================================


class ChatSummary(BaseModel):
    """Summary of a single chat/request for the list view.

    Issue #179: Shows chat history for the logged-in user.
    """

    request_id: str = Field(..., description="Unique request ID")
    timestamp: datetime = Field(..., description="When the request was made")
    model: str = Field(..., description="Model used for this request")
    input_tokens: int = Field(default=0, description="Number of input tokens")
    output_tokens: int = Field(default=0, description="Number of output tokens")
    cost_usd: Decimal = Field(default=Decimal("0.00"), description="Cost of this request")
    first_message_preview: str | None = Field(
        None,
        max_length=200,
        description="Preview of the first message (if chat logging enabled)",
    )
    stop_reason: str | None = Field(None, description="Reason the response stopped")


class ChatListResponse(BaseModel):
    """Response schema for listing user's chats.

    Issue #179: Paginated list of user's chat history.
    """

    chats: list[ChatSummary] = Field(default_factory=list, description="List of chat summaries")
    total: int = Field(..., description="Total number of chats")
    page: int = Field(..., description="Current page number")
    limit: int = Field(..., description="Number of items per page")


class ChatDetailResponse(BaseModel):
    """Response schema for a single chat detail.

    Issue #179: Full details of a specific chat/request.
    """

    request_id: str = Field(..., description="Unique request ID")
    timestamp: datetime = Field(..., description="When the request was made")
    model: str = Field(..., description="Model used for this request")
    input_tokens: int = Field(default=0, description="Number of input tokens")
    output_tokens: int = Field(default=0, description="Number of output tokens")
    cost_usd: Decimal = Field(default=Decimal("0.00"), description="Cost of this request")
    latency_ms: int = Field(default=0, description="Response latency in milliseconds")
    status_code: int = Field(default=200, description="HTTP status code")
    stop_reason: str | None = Field(None, description="Reason the response stopped")
    # Full conversation data (if chat logging is enabled)
    request_messages: list[dict[str, Any]] | None = Field(None, description="Full request messages (if chat logging enabled)")
    response_content: str | None = Field(None, description="Full response content (if chat logging enabled)")
    chat_logging_available: bool = Field(
        default=False,
        description="Whether full chat content is available (chat logging feature)",
    )


# =============================================================================
# Cognito Entity Schemas (Issue #226)
# =============================================================================


class CognitoUserResponse(BaseModel):
    """Response schema for a user from Cognito.

    Issue #226: Cognito as single source of truth for users.
    """

    username: str = Field(..., description="Cognito username (typically email)")
    email: str | None = Field(None, description="User email address")
    name: str | None = Field(None, description="User display name")
    github_username: str | None = Field(None, description="GitHub username from custom:github_username attribute")
    org_id: str | None = Field(None, description="Organization ID from custom:org_id attribute")
    department_id: str | None = Field(None, description="Department ID from custom:department_id attribute")
    team_id: str | None = Field(None, description="Team ID from custom:team_id attribute")
    role: str | None = Field(None, description="User role from custom:role attribute")
    status: str | None = Field(None, description="User status (CONFIRMED, FORCE_CHANGE_PASSWORD, etc.)")
    enabled: bool = Field(default=True, description="Whether the user is enabled")
    created_at: datetime | None = Field(None, description="When the user was created")
    updated_at: datetime | None = Field(None, description="When the user was last modified")


class CognitoUserListResponse(BaseModel):
    """Response schema for listing users from Cognito.

    Issue #226: Cognito as single source of truth for users.
    """

    items: list[CognitoUserResponse] = Field(default_factory=list, description="List of users")
    total: int = Field(..., description="Total number of users matching filter")
    page: int = Field(..., description="Current page number")
    page_size: int = Field(..., description="Number of items per page")
    has_more: bool = Field(..., description="Whether more pages exist")


class CognitoTeamResponse(BaseModel):
    """Response schema for a team/group from Cognito.

    Issue #226: Cognito groups represent teams.
    """

    group_name: str = Field(..., description="Cognito group name (team identifier)")
    description: str | None = Field(None, description="Group description")
    created_at: datetime | None = Field(None, description="When the group was created")
    updated_at: datetime | None = Field(None, description="When the group was last modified")


class CognitoTeamListResponse(BaseModel):
    """Response schema for listing teams from Cognito.

    Issue #226: Cognito groups represent teams.
    """

    items: list[CognitoTeamResponse] = Field(default_factory=list, description="List of teams")
    total: int = Field(..., description="Total number of teams matching filter")
    page: int = Field(..., description="Current page number")
    page_size: int = Field(..., description="Number of items per page")
    has_more: bool = Field(..., description="Whether more pages exist")


class CognitoDepartmentResponse(BaseModel):
    """Response schema for a department derived from Cognito users.

    Issue #226: Departments are derived from unique custom:department_id values
    across users in the organization.
    """

    department_id: str = Field(..., description="Department ID (from custom:department_id attribute)")


class CognitoDepartmentListResponse(BaseModel):
    """Response schema for listing departments from Cognito.

    Issue #226: Departments are derived from custom:department_id attribute values.
    """

    items: list[CognitoDepartmentResponse] = Field(default_factory=list, description="List of departments")
    total: int = Field(..., description="Total number of unique departments")
