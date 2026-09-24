"""Pydantic schemas for workspace endpoints."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CreateWorkspaceRequest(BaseModel):
    """POST /workspaces — create a new workspace."""

    operation_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    mode: str = Field(
        default="managed", pattern="^(managed|adopt|new-account-managed)$"
    )
    region: str | None = None
    cluster_reference: str | None = None
    plan_revision: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    approval_id: uuid.UUID | None = None
    account_email: str | None = None
    organizational_unit_id: str | None = None
    name: str = Field(..., min_length=1, max_length=255)
    isolation_mode: str = Field(
        default="dedicated", pattern="^(dedicated|namespace|research)$"
    )
    account: str | None = Field(
        default=None,
        description="AWS account name or ID — mandatory for research isolation mode",
    )
    quotas_json: str | None = Field(
        default=None, description="JSON string of quota overrides"
    )
    budget_max_daily_usd: Decimal | None = Field(
        default=None,
        ge=0,
        description="Maximum daily spend in USD (budget guardrail)",
    )
    budget_max_gpus: int | None = Field(
        default=None,
        ge=0,
        description="Maximum number of GPUs allowed (budget guardrail)",
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def research_requires_account(self) -> "CreateWorkspaceRequest":
        """Research isolation mode requires an AWS account."""
        if self.isolation_mode == "research" and not self.account:
            raise ValueError("Research workspaces require an AWS account (--account)")
        return self


class WorkspaceResponse(BaseModel):
    """Single workspace representation."""

    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    isolation_mode: str
    display_name: str = Field(description="Name with isolation tag for display")
    status: str
    provisioning_operation_id: str | None = None
    operation_state: str | None = None
    is_default: bool = Field(
        default=False, description="Platform default workspace (not deletable via CLI)"
    )
    budget_max_daily_usd: Decimal | None = None
    budget_max_hourly_usd: Decimal | None = None
    budget_max_gpus: int | None = None
    agent_iam_role_arn: str | None = None
    cluster_health: str | None = Field(
        default=None, description="Last known cluster health status"
    )
    last_heartbeat: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class WorkspaceListResponse(BaseModel):
    """GET /workspaces — list response."""

    workspaces: list[WorkspaceResponse]
    total: int


class KubeconfigResponse(BaseModel):
    """POST /workspaces/{id}/kubeconfig — scoped kubeconfig."""

    kubeconfig: str = Field(description="YAML kubeconfig with short-lived token")
    expires_at: datetime


class WorkspaceDeleteResponse(BaseModel):
    """DELETE /workspaces/{id} response."""

    id: uuid.UUID
    status: str = "Teardown"
    message: str = "Teardown workflow triggered"
