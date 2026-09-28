"""Workspace-cluster estimates from recorded nodes, separate from provider bills."""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workspace import Workspace
from app.services.node_cost_estimates import estimate, window, workspace_nodes


async def get_workspace_cost(
    workspace_id: uuid.UUID,
    org_id: uuid.UUID,
    db: AsyncSession,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    start, end = window(start_date, end_date, now)
    ws_result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id, Workspace.org_id == org_id
        )
    )
    workspace = ws_result.scalar_one_or_none()
    if workspace is None:
        return {"error": "Workspace not found", "status_code": 404}
    nodes = await workspace_nodes(db, workspace, start, end)
    return {
        "workspace_id": str(workspace_id),
        "workspace_name": workspace.name,
        **estimate(nodes, start, end).values,
        "currency": "USD",
        "cost_scope": "workspace_cluster",
        "checked_at": now.isoformat(),
        "period": {
            "start": start.isoformat() if start else None,
            "end": end.isoformat(),
        },
    }
