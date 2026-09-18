"""Cost aggregation service — queries heartbeat/node data from Postgres and aggregates per workspace."""

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.node import Node
from app.models.workspace import Workspace

logger = logging.getLogger(__name__)


async def get_workspace_cost(
    workspace_id: uuid.UUID,
    org_id: uuid.UUID,
    db: AsyncSession,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate cost data for a workspace from node heartbeat data.

    Cost is computed from:
    - Node hourly_cost_usd * hours running (created_at to terminated_at or now)
    - Grouped by GPU type and cloud provider

    Args:
        workspace_id: Target workspace.
        org_id: Owning organization (for access control).
        db: Async database session.
        start_date: Optional start of cost window.
        end_date: Optional end of cost window.

    Returns:
        Cost summary dict with total, breakdown by GPU type, and node details.
    """
    # Verify workspace belongs to org
    ws_result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.org_id == org_id,
        )
    )
    workspace = ws_result.scalar_one_or_none()
    if workspace is None:
        return {"error": "Workspace not found", "status_code": 404}

    if not workspace.cluster_id:
        return {
            "workspace_id": str(workspace_id),
            "workspace_name": workspace.name,
            "total_cost_usd": "0.00",
            "currency": "USD",
            "nodes": [],
            "breakdown_by_gpu": {},
            "breakdown_by_cloud": {},
            "period": {
                "start": start_date.isoformat() if start_date else None,
                "end": end_date.isoformat() if end_date else None,
            },
        }

    # Build node query with optional date filters
    filters = [Node.cluster_id == workspace.cluster_id]
    if start_date:
        filters.append(Node.created_at >= start_date)
    if end_date:
        # Nodes created before end_date
        filters.append(Node.created_at <= end_date)

    node_result = await db.execute(
        select(Node).where(and_(*filters)).order_by(Node.created_at.desc())
    )
    nodes = node_result.scalars().all()

    now = datetime.now(timezone.utc)
    total_cost = Decimal("0")
    node_details = []
    gpu_breakdown: dict[str, Decimal] = {}
    cloud_breakdown: dict[str, Decimal] = {}

    for node in nodes:
        hourly_rate = node.hourly_cost_usd or Decimal("0")

        # Calculate running hours
        start = node.created_at
        if start_date and start < start_date:
            start = start_date

        end = node.terminated_at or now
        if end_date and end > end_date:
            end = end_date

        if end <= start:
            hours = Decimal("0")
        else:
            delta = end - start
            hours = Decimal(str(delta.total_seconds())) / Decimal("3600")

        node_cost = hourly_rate * hours
        total_cost += node_cost

        # Track breakdown by GPU type
        gpu_key = node.gpu_type or "unknown"
        gpu_breakdown[gpu_key] = gpu_breakdown.get(gpu_key, Decimal("0")) + node_cost

        # Track breakdown by cloud provider
        cloud_key = node.cloud or "unknown"
        cloud_breakdown[cloud_key] = (
            cloud_breakdown.get(cloud_key, Decimal("0")) + node_cost
        )

        node_details.append(
            {
                "node_id": str(node.id),
                "name": node.k8s_node_name or node.instance_id or str(node.id),
                "gpu_type": node.gpu_type,
                "gpu_count": node.gpu_count,
                "cloud": node.cloud,
                "region": node.region,
                "hourly_cost_usd": str(hourly_rate),
                "hours_running": str(hours.quantize(Decimal("0.01"))),
                "total_cost_usd": str(node_cost.quantize(Decimal("0.01"))),
                "status": node.status,
                "created_at": node.created_at.isoformat() if node.created_at else None,
                "terminated_at": node.terminated_at.isoformat()
                if node.terminated_at
                else None,
            }
        )

    return {
        "workspace_id": str(workspace_id),
        "workspace_name": workspace.name,
        "total_cost_usd": str(total_cost.quantize(Decimal("0.01"))),
        "currency": "USD",
        "node_count": len(nodes),
        "nodes": node_details,
        "breakdown_by_gpu": {
            k: str(v.quantize(Decimal("0.01"))) for k, v in gpu_breakdown.items()
        },
        "breakdown_by_cloud": {
            k: str(v.quantize(Decimal("0.01"))) for k, v in cloud_breakdown.items()
        },
        "period": {
            "start": start_date.isoformat() if start_date else None,
            "end": end_date.isoformat() if end_date else None,
        },
    }
