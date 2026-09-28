"""Pydantic schemas for quota management endpoints."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class QuotaLimits(BaseModel):
    """Quota configuration — shared between org-level and workspace-level."""

    max_gpus: int | None = Field(
        default=None, ge=0, description="Maximum total GPUs allowed"
    )
    max_cost_per_day: Decimal | None = Field(
        default=None,
        ge=0,
        description="Maximum daily spend in USD",
    )
    max_workspaces: int | None = Field(
        default=None,
        ge=0,
        description="Maximum number of active workspaces (org-level only)",
    )
    max_nodes: int | None = Field(
        default=None, ge=0, description="Maximum number of nodes"
    )
    allowed_clouds: list[str] | None = Field(
        default=None,
        description="List of allowed cloud providers (e.g., ['aws', 'gcp', 'lambda'])",
    )


class SetWorkspaceQuotaRequest(BaseModel):
    """PATCH /workspaces/{id}/quota — set per-workspace quotas."""

    max_gpus: int | None = Field(
        default=None, ge=0, description="Maximum total GPUs allowed"
    )
    max_cost_per_day: Decimal | None = Field(
        default=None,
        ge=0,
        description="Maximum daily spend in USD",
    )
    max_nodes: int | None = Field(
        default=None, ge=0, description="Maximum number of nodes"
    )
    allowed_clouds: list[str] | None = Field(
        default=None,
        description="List of allowed cloud providers (e.g., ['aws', 'gcp', 'lambda'])",
    )


class SetOrgQuotaRequest(BaseModel):
    """PATCH /orgs/current/quota — set org-wide quotas."""

    max_gpus: int | None = Field(
        default=None, ge=0, description="Maximum total GPUs across all workspaces"
    )
    max_cost_per_day: Decimal | None = Field(
        default=None,
        ge=0,
        description="Maximum daily spend in USD across all workspaces",
    )
    max_workspaces: int | None = Field(
        default=None, ge=0, description="Maximum number of active workspaces"
    )
    max_nodes: int | None = Field(
        default=None, ge=0, description="Maximum total nodes across all workspaces"
    )
    allowed_clouds: list[str] | None = Field(
        default=None,
        description="Allowed cloud providers for all workspaces",
    )


class QuotaUsage(BaseModel):
    """Current usage against quota limits."""

    current_gpus: int = 0
    current_cost_today_usd: str = "0.00"
    current_workspaces: int = 0
    current_nodes: int = 0


class WorkspaceQuotaResponse(BaseModel):
    """Response for workspace quota endpoints."""

    workspace_id: uuid.UUID
    workspace_name: str
    quotas: QuotaLimits
    usage: QuotaUsage
    within_limits: bool = True
    violations: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


class OrgQuotaResponse(BaseModel):
    """Response for org quota endpoints."""

    org_id: uuid.UUID
    org_name: str
    billing_plan: str
    quotas: QuotaLimits
    usage: QuotaUsage
    within_limits: bool = True
    violations: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


class QuotaExceededDetail(BaseModel):
    """Detail payload for 429 quota exceeded responses."""

    detail: str
    quota_type: str
    current_value: str
    limit_value: str
    resource_type: str = "workspace"
    resource_id: str | None = None
