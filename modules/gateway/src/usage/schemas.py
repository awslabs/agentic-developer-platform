"""Usage module Pydantic schemas for API request/response validation."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from src.usage.config import AggregationInterval


class UsageRecordRequest(BaseModel):
    """Request schema for recording usage."""

    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: Decimal = Field(ge=0)
    latency_ms: int = Field(ge=0)
    status_code: int
    request_id: str | None = None
    bedrock_account_id: str | None = None


class UsageSummaryResponse(BaseModel):
    """Response schema for usage summary."""

    org_id: str
    start_date: datetime
    end_date: datetime
    total_requests: int
    successful_requests: int
    failed_requests: int
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    total_cost_usd: Decimal
    average_latency_ms: float
    error_rate_percent: float
    unique_users: int
    unique_models: int


class UsageByOrganizationResponse(BaseModel):
    """Response schema for usage by organization."""

    org_id: str
    org_name: str | None
    total_requests: int
    total_tokens: int
    total_cost_usd: Decimal
    average_latency_ms: float
    error_rate_percent: float
    period_start: datetime
    period_end: datetime


class UsageByModelResponse(BaseModel):
    """Response schema for usage by model."""

    model: str
    total_requests: int
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    total_cost_usd: Decimal
    average_latency_ms: float
    error_rate_percent: float


class UsageTimelineEntry(BaseModel):
    """Response schema for a single timeline entry."""

    timestamp: datetime
    interval: AggregationInterval
    total_requests: int
    total_tokens: int
    total_cost_usd: Decimal
    average_latency_ms: float
    error_count: int


class UsageTimelineResponse(BaseModel):
    """Response schema for usage timeline."""

    org_id: str | None
    start_date: datetime
    end_date: datetime
    interval: AggregationInterval
    data: list[UsageTimelineEntry]


class UsageByUserResponse(BaseModel):
    """Response schema for usage by user."""

    user_id: str
    account_type: str
    total_requests: int
    total_tokens: int
    total_cost_usd: Decimal
    last_request_at: datetime | None


class UsageByDepartmentResponse(BaseModel):
    """Response schema for usage by department."""

    department_id: str
    department_name: str | None
    total_requests: int
    total_tokens: int
    total_cost_usd: Decimal
    unique_users: int
    top_models: list[str]


class UsageByTeamResponse(BaseModel):
    """Response schema for usage by team."""

    team_id: str
    team_name: str | None
    department_id: str
    total_requests: int
    total_tokens: int
    total_cost_usd: Decimal
    unique_users: int


class UsageExportRequest(BaseModel):
    """Request schema for usage export."""

    org_id: str | None = None
    start_date: datetime
    end_date: datetime
    include_details: bool = False
    format: str = "json"  # json, csv


class UsageFilters(BaseModel):
    """Common filters for usage queries."""

    org_id: str | None = None
    department_id: str | None = None
    team_id: str | None = None
    user_id: str | None = None
    model: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    status_code: int | None = None
    min_latency_ms: int | None = None
    max_latency_ms: int | None = None


class UsageListResponse(BaseModel):
    """Generic response for paginated usage data."""

    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int
    has_more: bool


class UsageLogResponse(BaseModel):
    """Response schema for a single usage log entry."""

    id: str
    timestamp: datetime
    org_id: str
    department_id: str
    team_id: str
    user_id: str
    account_type: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    latency_ms: int
    status_code: int
    request_id: str | None
    bedrock_account_id: str | None

    class Config:
        from_attributes = True
