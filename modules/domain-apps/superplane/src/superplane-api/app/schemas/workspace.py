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
    # Issue #6048: the explicit dedicated/shared placement choice. Defaults to
    # "dedicated" so an old client that has never heard of shared placement keeps
    # its existing behavior exactly — see DESIGN.md's "default dedicated behavior
    # remains" requirement. Never inferred from `cluster_reference`: that field
    # is the pre-existing "adopt an existing cluster as MY dedicated cluster"
    # input, and naming a cluster there must not silently opt into sharing it.
    cluster_placement: str = Field(default="dedicated", pattern="^(dedicated|shared)$")
    # Opaque cluster identifier, required for `cluster_placement == "shared"` and
    # forbidden otherwise. Resolved and verified server-side under the caller's
    # authenticated organization (see `app/services/cluster_sharing.py`) — this
    # field is a selection, never itself proof of eligibility or ownership.
    shared_cluster_id: uuid.UUID | None = None
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

    @model_validator(mode="after")
    def shared_placement_names_exactly_one_cluster(self) -> "CreateWorkspaceRequest":
        """Shared placement requires a cluster selection; dedicated forbids one.

        A `shared_cluster_id` supplied alongside dedicated placement would be a
        selection with no expressed intent to use it, which is exactly the kind
        of ambiguous input DESIGN.md says must be refused rather than guessed at.
        """
        if self.cluster_placement == "shared" and self.shared_cluster_id is None:
            raise ValueError("shared cluster placement requires shared_cluster_id")
        if self.cluster_placement == "dedicated" and self.shared_cluster_id is not None:
            raise ValueError(
                "shared_cluster_id is only valid with cluster_placement=shared"
            )
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


class EligibleClusterResponse(BaseModel):
    """One cluster the caller's organization may select for shared placement."""

    id: uuid.UUID
    name: str
    cluster_arn: str | None = None
    platform_eligible: bool = Field(
        description="Whether this is ADP's management cluster, explicitly authorized"
        " for tenant placement"
    )
    member_count: int = Field(description="Current live (non-removed) member count")


class EligibleClusterListResponse(BaseModel):
    """GET /workspaces?view=eligible-clusters — response.

    Issue #6048. Shared placement selection at workspace creation lists only
    what this endpoint returns: clusters explicitly `sharing_enabled` under the
    caller's own authenticated organization. See `app/services/cluster_sharing.py`.
    """

    clusters: list[EligibleClusterResponse]


class KubeconfigResponse(BaseModel):
    """POST /workspaces/{id}/kubeconfig — scoped kubeconfig."""

    kubeconfig: str = Field(description="YAML kubeconfig with short-lived token")
    expires_at: datetime


class WorkspaceDeleteResponse(BaseModel):
    """DELETE /workspaces/{id} response."""

    id: uuid.UUID
    status: str = "Teardown"
    message: str = "Teardown workflow triggered"
