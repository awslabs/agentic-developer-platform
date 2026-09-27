"""Pydantic schemas for proxy, deployment, heartbeat, and cost endpoints."""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --- Node schemas ---


class NodeInfo(BaseModel):
    """Single K8s node from child cluster."""

    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    ready: str = "Unknown"
    cpu_allocatable: str = "0"
    memory_allocatable: str = "0"
    gpu_allocatable: str = "0"
    instance_type: str = "unknown"
    zone: str = "unknown"
    created_at: str | None = None


class NodeListResponse(BaseModel):
    """GET /workspaces/{id}/nodes response."""

    workspace_id: uuid.UUID
    nodes: list[NodeInfo]
    total: int


# --- Deployment schemas ---


class CreateDeploymentRequest(BaseModel):
    """POST /workspaces/{id}/deployments — create a model deployment."""

    operation_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    profile_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,62}$")
    approval_id: uuid.UUID | None = None
    plan_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    name: str = Field(
        ..., min_length=1, max_length=255, pattern="^[a-z0-9][a-z0-9-]*[a-z0-9]$"
    )
    model_name: str = Field(
        ..., min_length=1, max_length=500, description="HuggingFace model name"
    )
    expected_namespace: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    precision: str = Field(default="fp16", pattern="^(fp16|bf16|fp8|awq|int8)$")
    serving_framework: str = Field(default="vllm", pattern="^(vllm|sglang)$")
    replicas: int = Field(default=1, ge=1, le=32)
    gpu_per_replica: int = Field(default=1, ge=1, le=8)
    tensor_parallel_size: int = Field(default=1, ge=1, le=8)
    max_model_len: int | None = Field(default=None, ge=256, le=1048576)

    # `namespace` was REMOVED, not ignored (issue #5671, A15). It used to select the
    # Kubernetes namespace the deployment was created in, so on a cluster shared by
    # several workspaces a caller could place — or overwrite — a workload in a
    # neighbour's namespace. The namespace is now resolved server-side from the
    # workspace record (`app.services.workspace_namespace`).
    #
    # Removing the field rather than accepting-and-discarding it is deliberate: a
    # silently ignored field leaves automation believing it still chooses the
    # namespace, and the request keeps looking like it worked as intended. `extra`
    # below makes supplying it an explicit 422 instead.
    model_config = ConfigDict(extra="forbid")


class CancelWorkloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(min_length=1, max_length=255)


class DeleteDeploymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: uuid.UUID
    approval_id: uuid.UUID | None = None
    plan_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    cleanup_mode: Literal["aggregate", "staged-v1"] = "aggregate"


class BatchOptions(BaseModel):
    """Exact immutable invocation selected from an installed batch profile."""

    model_config = ConfigDict(extra="forbid", strict=True)
    image: str = Field(
        pattern=r"^[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}$", max_length=512
    )
    command: list[str] = Field(min_length=1, max_length=32)
    args: list[str] = Field(max_length=32)
    gpu_count: int = Field(ge=1, le=8)
    cpu: str = Field(pattern=r"^[1-9][0-9]{0,4}m?$")
    memory: str = Field(pattern=r"^[1-9][0-9]{0,4}[MG]i$")


class CreateBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: uuid.UUID
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    approval_id: uuid.UUID | None = None
    plan_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,50}$")
    batch_options: BatchOptions


class DeploymentInfo(BaseModel):
    deployment_id: uuid.UUID | None = None
    operation_id: str | None = None
    operation_state: str | None = None
    cancellation_requested: bool = False
    cleanup_status: str = "unconfirmed"
    provider_uid: str | None = None
    """Single deployment info."""

    name: str
    namespace: str = "default"
    replicas: int = 0
    ready_replicas: int = 0
    available_replicas: int = 0
    labels: dict[str, str] = Field(default_factory=dict)
    created_at: str | None = None
    status: str | None = None


class DeploymentCreateResponse(BaseModel):
    """POST /workspaces/{id}/deployments response."""

    name: str
    namespace: str
    replicas: int
    status: str
    deployment_id: uuid.UUID | None = None
    operation_id: str | None = None
    operation_state: str | None = None
    cancellation_requested: bool = False
    cleanup_status: str = "unconfirmed"
    provider_uid: str | None = None


class DeploymentListResponse(BaseModel):
    """GET /workspaces/{id}/deployments response."""

    workspace_id: uuid.UUID
    deployments: list[DeploymentInfo]
    total: int


class DeploymentDeleteResponse(BaseModel):
    """DELETE /workspaces/{id}/deployments/{dep_id} response."""

    name: str
    namespace: str
    status: str = "Deleting"
    operation_id: str | None = None
    operation_state: str | None = None
    cancellation_requested: bool = False
    cleanup_status: str = "unconfirmed"


# --- Heartbeat schemas ---


class NodeSummary(BaseModel):
    """Aggregated node health summary from data plane controller."""

    total: int = Field(default=0, ge=0)
    ready: int = Field(default=0, ge=0)
    not_ready: int = Field(default=0, ge=0)


class HeartbeatRequest(BaseModel):
    """POST /internal/heartbeat — from data plane Superplane Controller.

    The actual_state_json blob is stored as-is and consumed by the
    ClusterHealthReconciler for multi-dimension health evaluation:
      - skypilot_healthy: bool
      - vault_sync_status: str (ok | failed | pending)
      - node_summary: NodeSummary
      - cost_hourly: float
      - cost_hourly_avg: float
    """

    cluster_id: uuid.UUID
    health_status: str = Field(..., pattern="^(Healthy|Degraded|Unhealthy|Unknown)$")
    actual_state_json: dict[str, Any] = Field(
        default_factory=dict,
        description="Current cluster state reported by controller",
    )
    node_count: int | None = Field(default=None, ge=0)
    gpu_count: int | None = Field(default=None, ge=0)
    deployment_count: int | None = Field(default=None, ge=0)
    controller_version: str | None = None
    uptime_seconds: int | None = Field(default=None, ge=0)

    # --- Fields consumed by ClusterHealthReconciler (US-H1) ---
    skypilot_healthy: bool | None = Field(
        default=None,
        description="Whether the SkyPilot pod is running on the data plane",
    )
    vault_sync_status: str | None = Field(
        default=None,
        pattern="^(ok|failed|pending)$",
        description="Vault credential sync status",
    )
    node_summary: NodeSummary | None = Field(
        default=None,
        description="Aggregated node health summary",
    )
    cost_hourly: float | None = Field(
        default=None,
        ge=0,
        description="Current hourly cost in USD",
    )
    cost_hourly_avg: float | None = Field(
        default=None,
        ge=0,
        description="Rolling average hourly cost in USD",
    )


class HeartbeatResponse(BaseModel):
    """POST /internal/heartbeat response."""

    cluster_id: uuid.UUID
    accepted: bool = True
    previous_health_status: str | None = None
    current_health_status: str
    message: str = "Heartbeat accepted"


# --- Cost schemas ---


class CostEstimateInfo(BaseModel):
    """Recorded node estimates are distinct from reconciled provider charges."""

    cost_basis: str = "recorded_node_rates"
    cost_scope: str = "workspace_cluster"
    estimate_status: str = "unavailable"
    known_subtotal_usd: str = "0.00"
    observed_cost_usd: str | None = None
    cost_reconciliation: str = "unavailable"
    unestimated_node_count: int = 0
    checked_at: str | None = None


class CostNodeDetail(BaseModel):
    """Cost detail for a single node."""

    node_id: str
    name: str
    gpu_type: str | None = None
    gpu_count: int | None = None
    cloud: str | None = None
    region: str | None = None
    hourly_cost_usd: str | None = None
    hours_running: str | None = None
    total_cost_usd: str | None = None
    status: str
    created_at: str | None = None
    terminated_at: str | None = None


class CostPeriod(BaseModel):
    """Cost query time period."""

    start: str | None = None
    end: str | None = None


class CostResponse(CostEstimateInfo):
    """GET /workspaces/{id}/cost response."""

    workspace_id: str
    workspace_name: str
    total_cost_usd: str | None = None
    currency: str = "USD"
    node_count: int = 0
    nodes: list[CostNodeDetail] = Field(default_factory=list)
    breakdown_by_gpu: dict[str, str | None] = Field(default_factory=dict)
    breakdown_by_cloud: dict[str, str | None] = Field(default_factory=dict)
    period: CostPeriod = Field(default_factory=CostPeriod)


# --- Org cost schemas ---


class OrgWorkspaceCost(CostEstimateInfo):
    """Cost summary for a single workspace within an org cost response."""

    workspace_id: str
    workspace_name: str
    total_cost_usd: str | None = None
    node_count: int = 0
    status: str | None = None
    budget_max_daily_usd: str | None = None
    budget_max_gpus: int | None = None


class BudgetAlertInfo(BaseModel):
    """Active budget alert summary."""

    id: str
    workspace_id: str
    alert_type: str
    severity: str
    message: str | None = None
    current_value: str | None = None
    limit_value: str | None = None
    created_at: str | None = None


class OrgCostResponse(CostEstimateInfo):
    """GET /orgs/cost response — org-level cost aggregation."""

    org_id: str
    total_cost_usd: str | None = None
    currency: str = "USD"
    workspace_count: int = 0
    workspaces: list[OrgWorkspaceCost] = Field(default_factory=list)
    breakdown_by_gpu: dict[str, str | None] = Field(default_factory=dict)
    breakdown_by_cloud: dict[str, str | None] = Field(default_factory=dict)
    active_alerts: list[BudgetAlertInfo] = Field(default_factory=list)
    period: CostPeriod = Field(default_factory=CostPeriod)


# --- Budget status schemas ---


class BudgetInfo(CostEstimateInfo):
    """Current budget configuration and usage."""

    max_daily_usd: str | None = None
    max_gpus: int | None = None
    current_daily_cost_usd: str | None = None
    current_active_gpus: int = 0
    daily_budget_used_pct: str | None = None


class WorkspaceBudgetAlertInfo(BaseModel):
    """Budget alert for a workspace."""

    id: str
    alert_type: str
    severity: str
    message: str | None = None
    created_at: str | None = None


class BudgetStatusResponse(BaseModel):
    """GET /workspaces/{id}/budget response."""

    workspace_id: str
    workspace_name: str
    status: str
    budget: BudgetInfo = Field(default_factory=BudgetInfo)
    active_alerts: list[WorkspaceBudgetAlertInfo] = Field(default_factory=list)


class ReconcileResponse(BaseModel):
    """POST /internal/cost-reconcile response."""

    status: str
    workspaces_checked: int = 0
    warnings_created: int = 0
    violations_created: int = 0
    workspaces_suspended: int = 0
    timestamp: str | None = None
    reason: str | None = None
