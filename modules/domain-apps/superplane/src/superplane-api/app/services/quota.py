"""Quota enforcement service — validates resource requests against org and workspace quotas.

Quotas are stored as JSON in the quotas_json column on both organizations and workspaces.
This service parses those JSON blobs and enforces limits before resource creation.

Enforcement points:
1. Workspace creation  -> check org max_workspaces
2. Node provisioning   -> check workspace max_nodes, max_gpus, allowed_clouds
3. Deployment creation -> check workspace max_gpus (gpu_per_replica * replicas)
4. Budget (daily cost) -> checked by CostReconciler, but also pre-checked here
"""

import json
import logging
import uuid
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.node import Node
from app.models.organization import Organization
from app.models.workspace import Workspace

logger = logging.getLogger(__name__)

# Default plan-based quotas (fallback when no explicit quota is set)
PLAN_DEFAULTS: dict[str, dict[str, Any]] = {
    "free": {
        "max_workspaces": 1,
        "max_nodes": 2,
        "max_gpus": 4,
        "max_cost_per_day": 50,
    },
    "pro": {
        "max_workspaces": 5,
        "max_nodes": 20,
        "max_gpus": 32,
        "max_cost_per_day": 1000,
    },
    "enterprise": {
        "max_workspaces": 50,
        "max_nodes": 200,
        "max_gpus": 256,
        "max_cost_per_day": 10000,
    },
}


def parse_quotas(quotas_json: str | None, plan: str = "free") -> dict[str, Any]:
    """Parse quotas from JSON string, falling back to plan defaults."""
    if quotas_json:
        try:
            return json.loads(quotas_json)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Invalid quotas_json, using plan defaults: %s", quotas_json)
    return PLAN_DEFAULTS.get(plan, PLAN_DEFAULTS["free"]).copy()


def merge_quotas(existing: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """Merge quota updates into existing quotas. Only non-None values are updated."""
    merged = existing.copy()
    for key, value in updates.items():
        if value is not None:
            merged[key] = value
    return merged


async def get_org_quotas(
    org_id: uuid.UUID, db: AsyncSession
) -> tuple[Organization, dict[str, Any]]:
    """Load org and its effective quotas.

    Returns:
        Tuple of (Organization, quota dict).

    Raises:
        HTTPException 404 if org not found.
    """
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found"
        )

    quotas = parse_quotas(org.quotas_json, org.billing_plan)
    return org, quotas


async def get_workspace_quotas(
    workspace_id: uuid.UUID,
    org_id: uuid.UUID,
    db: AsyncSession,
) -> tuple[Workspace, dict[str, Any]]:
    """Load workspace and its effective quotas.

    Workspace quotas inherit from org quotas, with workspace-level overrides.

    Returns:
        Tuple of (Workspace, quota dict).

    Raises:
        HTTPException 404 if workspace not found.
    """
    result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id, Workspace.org_id == org_id
        )
    )
    workspace = result.scalar_one_or_none()
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found"
        )

    # Start with org-level quotas
    _, org_quotas = await get_org_quotas(org_id, db)

    # Overlay workspace-level quotas
    ws_quotas = parse_quotas(workspace.quotas_json, "free")
    effective = org_quotas.copy()
    for key, val in ws_quotas.items():
        if val is not None:
            effective[key] = val

    # Also use budget fields from workspace model directly
    if workspace.budget_max_daily_usd is not None:
        effective["max_cost_per_day"] = float(workspace.budget_max_daily_usd)
    if workspace.budget_max_gpus is not None:
        effective["max_gpus"] = workspace.budget_max_gpus

    return workspace, effective


async def count_org_workspaces(org_id: uuid.UUID, db: AsyncSession) -> int:
    """Count active (non-teardown/deleted) workspaces for an org."""
    result = await db.execute(
        select(func.count(Workspace.id)).where(
            Workspace.org_id == org_id,
            Workspace.status.notin_(["Teardown", "Deleted"]),
        )
    )
    return int(result.scalar() or 0)


async def count_workspace_nodes(workspace: Workspace, db: AsyncSession) -> int:
    """Count active nodes for a workspace."""
    if not workspace.cluster_id:
        return 0
    result = await db.execute(
        select(func.count(Node.id)).where(
            Node.cluster_id == workspace.cluster_id,
            Node.terminated_at.is_(None),
            Node.status.in_(["Running", "Provisioning", "Ready"]),
        )
    )
    return int(result.scalar() or 0)


async def count_workspace_gpus(workspace: Workspace, db: AsyncSession) -> int:
    """Count active GPUs for a workspace."""
    if not workspace.cluster_id:
        return 0
    result = await db.execute(
        select(func.coalesce(func.sum(Node.gpu_count), 0)).where(
            Node.cluster_id == workspace.cluster_id,
            Node.terminated_at.is_(None),
            Node.status.in_(["Running", "Provisioning", "Ready"]),
        )
    )
    return int(result.scalar() or 0)


async def count_org_gpus(org_id: uuid.UUID, db: AsyncSession) -> int:
    """Count total active GPUs across all workspaces for an org."""
    result = await db.execute(
        select(func.coalesce(func.sum(Node.gpu_count), 0)).where(
            Node.org_id == org_id,
            Node.terminated_at.is_(None),
            Node.status.in_(["Running", "Provisioning", "Ready"]),
        )
    )
    return int(result.scalar() or 0)


async def count_org_nodes(org_id: uuid.UUID, db: AsyncSession) -> int:
    """Count total active nodes across all workspaces for an org."""
    result = await db.execute(
        select(func.count(Node.id)).where(
            Node.org_id == org_id,
            Node.terminated_at.is_(None),
            Node.status.in_(["Running", "Provisioning", "Ready"]),
        )
    )
    return int(result.scalar() or 0)


def raise_quota_exceeded(
    quota_type: str,
    current: int | float | Decimal,
    limit: int | float | Decimal,
    resource_type: str = "workspace",
    resource_id: str | None = None,
) -> None:
    """Raise HTTP 429 with a clear quota exceeded message.

    Args:
        quota_type: Type of quota (e.g., 'max_gpus', 'max_workspaces').
        current: Current usage value.
        limit: Quota limit value.
        resource_type: Type of resource being constrained.
        resource_id: Optional ID of the resource.
    """
    detail = (
        f"Quota exceeded: {quota_type} limit is {limit}, "
        f"current usage is {current}. "
        f"Contact your platform administrator to increase the quota."
    )
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={
            "X-Quota-Type": quota_type,
            "X-Quota-Current": str(current),
            "X-Quota-Limit": str(limit),
            "Retry-After": "60",
        },
    )


async def enforce_workspace_creation_quota(org_id: uuid.UUID, db: AsyncSession) -> None:
    """Check that the org can create another workspace.

    Raises:
        HTTPException 429 if workspace quota is exceeded.
    """
    _, quotas = await get_org_quotas(org_id, db)
    max_workspaces = quotas.get("max_workspaces")

    if max_workspaces is not None:
        current = await count_org_workspaces(org_id, db)
        if current >= max_workspaces:
            raise_quota_exceeded(
                "max_workspaces", current, max_workspaces, "org", str(org_id)
            )


async def enforce_node_provisioning_quota(
    workspace_id: uuid.UUID,
    org_id: uuid.UUID,
    gpu_count: int,
    cloud: str | None,
    db: AsyncSession,
) -> None:
    """Check quotas before provisioning a node.

    Validates:
    - Workspace max_nodes
    - Workspace max_gpus (current + requested)
    - Org-level max_nodes and max_gpus
    - Allowed clouds

    Raises:
        HTTPException 429 if any quota is exceeded.
    """
    workspace, ws_quotas = await get_workspace_quotas(workspace_id, org_id, db)

    # Check workspace max_nodes
    max_nodes = ws_quotas.get("max_nodes")
    if max_nodes is not None:
        current_nodes = await count_workspace_nodes(workspace, db)
        if current_nodes + 1 > max_nodes:
            raise_quota_exceeded(
                "max_nodes", current_nodes, max_nodes, "workspace", str(workspace_id)
            )

    # Check workspace max_gpus
    max_gpus = ws_quotas.get("max_gpus")
    if max_gpus is not None:
        current_gpus = await count_workspace_gpus(workspace, db)
        if current_gpus + gpu_count > max_gpus:
            raise_quota_exceeded(
                "max_gpus", current_gpus, max_gpus, "workspace", str(workspace_id)
            )

    # Check allowed clouds
    allowed_clouds = ws_quotas.get("allowed_clouds")
    if allowed_clouds and cloud:
        if cloud.lower() not in [c.lower() for c in allowed_clouds]:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Cloud provider '{cloud}' is not allowed. Allowed: {allowed_clouds}",
                headers={"X-Quota-Type": "allowed_clouds"},
            )

    # Check org-level quotas
    _, org_quotas = await get_org_quotas(org_id, db)

    org_max_nodes = org_quotas.get("max_nodes")
    if org_max_nodes is not None:
        org_nodes = await count_org_nodes(org_id, db)
        if org_nodes + 1 > org_max_nodes:
            raise_quota_exceeded(
                "max_nodes", org_nodes, org_max_nodes, "org", str(org_id)
            )

    org_max_gpus = org_quotas.get("max_gpus")
    if org_max_gpus is not None:
        org_gpus = await count_org_gpus(org_id, db)
        if org_gpus + gpu_count > org_max_gpus:
            raise_quota_exceeded("max_gpus", org_gpus, org_max_gpus, "org", str(org_id))


async def enforce_deployment_quota(
    workspace_id: uuid.UUID,
    org_id: uuid.UUID,
    total_gpus_requested: int,
    db: AsyncSession,
) -> None:
    """Check GPU quotas before creating a deployment.

    Args:
        workspace_id: Target workspace.
        org_id: Owning organization.
        total_gpus_requested: Total GPU count for the deployment (replicas * gpu_per_replica).
        db: Async database session.

    Raises:
        HTTPException 429 if GPU quota would be exceeded.
    """
    workspace, ws_quotas = await get_workspace_quotas(workspace_id, org_id, db)

    max_gpus = ws_quotas.get("max_gpus")
    if max_gpus is not None:
        current_gpus = await count_workspace_gpus(workspace, db)
        if current_gpus + total_gpus_requested > max_gpus:
            raise_quota_exceeded(
                "max_gpus",
                current_gpus,
                max_gpus,
                "workspace",
                str(workspace_id),
            )
