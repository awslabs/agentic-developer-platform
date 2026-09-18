"""Heartbeat ingestion endpoint — receives health data from data plane Superplane Controllers.

Route:
    POST /internal/heartbeat
"""

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.cluster import Cluster
from app.models.event import Event
from app.routers.internal import verify_internal_token
from app.schemas.proxy import HeartbeatRequest, HeartbeatResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post(
    "/heartbeat",
    response_model=HeartbeatResponse,
    # Issue #5055 (U14). This route had NO authentication, while three of its
    # five siblings under the same `/internal` prefix enforced the shared token
    # and the module docstring already claimed the prefix was machine-to-machine
    # only. Unauthenticated, anyone able to reach the service could write cluster
    # health for any cluster id — which drives the reconciler and the Degraded
    # transitions computed from `last_heartbeat`.
    dependencies=[Depends(verify_internal_token)],
)
async def ingest_heartbeat(
    body: HeartbeatRequest,
    db: AsyncSession = Depends(get_session),
) -> HeartbeatResponse:
    """Ingest heartbeat from data plane Superplane Controller.

    - Validates cluster_id exists in DB
    - Updates clusters table: health_status, last_heartbeat, actual_state_json
    - Inserts event on state transitions (health status changes)
    """
    # Look up cluster
    result = await db.execute(select(Cluster).where(Cluster.id == body.cluster_id))
    cluster = result.scalar_one_or_none()

    if cluster is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cluster {body.cluster_id} not found",
        )

    # Track previous state for transition detection
    previous_health_status = cluster.health_status

    # Merge reconciler-relevant fields into actual_state_json for ClusterHealthReconciler.
    enriched_state = dict(body.actual_state_json)
    if body.skypilot_healthy is not None:
        enriched_state["skypilot_healthy"] = body.skypilot_healthy
    if body.vault_sync_status is not None:
        enriched_state["vault_sync_status"] = body.vault_sync_status
    if body.node_summary is not None:
        enriched_state["node_summary"] = body.node_summary.model_dump()
    if body.cost_hourly is not None:
        enriched_state["cost_hourly"] = body.cost_hourly
    if body.cost_hourly_avg is not None:
        enriched_state["cost_hourly_avg"] = body.cost_hourly_avg

    # Update cluster
    cluster.health_status = body.health_status
    cluster.last_heartbeat = datetime.now(timezone.utc)
    cluster.actual_state_json = enriched_state

    # Detect state transition and insert event
    health_changed = previous_health_status != body.health_status
    if health_changed and previous_health_status is not None:
        event = Event(
            org_id=cluster.org_id,
            resource_type="cluster",
            resource_id=cluster.id,
            event_type="health_status_changed",
            message=f"Cluster health changed from {previous_health_status} to {body.health_status}",
            details_json=json.dumps(
                {
                    "previous_health_status": previous_health_status,
                    "new_health_status": body.health_status,
                    "node_count": body.node_count,
                    "gpu_count": body.gpu_count,
                    "deployment_count": body.deployment_count,
                    "controller_version": body.controller_version,
                }
            ),
        )
        db.add(event)
        logger.info(
            "Cluster %s health transitioned: %s -> %s",
            cluster.id,
            previous_health_status,
            body.health_status,
        )

    await db.commit()

    return HeartbeatResponse(
        cluster_id=body.cluster_id,
        accepted=True,
        previous_health_status=previous_health_status,
        current_health_status=body.health_status,
        message="Heartbeat accepted"
        + (" (health status changed)" if health_changed else ""),
    )
