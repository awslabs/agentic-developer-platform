"""Internal API endpoints — used by bootstrap workflows and control plane services.

These endpoints are authenticated via a shared API_SERVER_TOKEN (bearer token),
NOT via Cognito JWT. They are intended for machine-to-machine use only.
"""

import json
import logging
import secrets
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.models.cluster import Cluster

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


class ClusterResourceUpdate(BaseModel):
    """Payload for updating cluster resource URLs (from bootstrap workflow)."""

    agent_input_queue_url: str | None = None
    agent_response_queue_url: str | None = None
    assume_role_arn: str | None = None


async def verify_internal_token(
    authorization: str = Header(..., alias="Authorization"),
) -> None:
    """Verify the internal API token from the Authorization header.

    Expects: Authorization: Bearer <API_SERVER_TOKEN>
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )

    token = authorization[len("Bearer ") :]
    if not settings.internal_api_token or not secrets.compare_digest(
        token, settings.internal_api_token
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal API token",
        )


@router.patch(
    "/clusters/{cluster_id}/resources",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(verify_internal_token)],
)
async def update_cluster_resources(
    cluster_id: uuid.UUID,
    body: ClusterResourceUpdate,
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Update a cluster's actual_state_json with resource URLs.

    Called by the bootstrap workflow after provisioning data plane resources
    (SQS queues, IAM roles). Merges the provided fields into the existing
    actual_state_json, preserving any previously set values.
    """
    result = await db.execute(select(Cluster).where(Cluster.id == cluster_id))
    cluster = result.scalar_one_or_none()

    if cluster is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cluster {cluster_id} not found",
        )

    # Merge new resource data into existing state
    existing_state = {}
    if cluster.actual_state_json:
        try:
            existing_state = (
                cluster.actual_state_json
                if isinstance(cluster.actual_state_json, dict)
                else json.loads(cluster.actual_state_json)
            )
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Failed to parse existing actual_state_json for cluster %s, starting fresh",
                cluster_id,
            )

    # Only update fields that are explicitly provided (not None)
    updates = body.model_dump(exclude_none=True)
    existing_state.update(updates)

    cluster.actual_state_json = existing_state
    await db.commit()

    logger.info(
        "Updated cluster %s resources: %s",
        cluster_id,
        list(updates.keys()),
    )

    return {
        "status": "ok",
        "cluster_id": str(cluster_id),
        "updated_fields": list(updates.keys()),
    }


# --- Vault Sync Manual Trigger ---


class VaultSyncTriggerRequest(BaseModel):
    """Payload for manually triggering a vault sync."""

    credential_id: uuid.UUID | None = None
    cluster_id: uuid.UUID | None = None


@router.post(
    "/vault-sync/trigger",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(verify_internal_token)],
)
async def trigger_vault_sync(body: VaultSyncTriggerRequest) -> dict:
    """Manually trigger credential sync to data plane clusters.

    Optionally filter by credential_id and/or cluster_id.
    If neither is provided, syncs all pending/stale assignments.

    Used by:
      - Admin CLI for manual re-sync after credential rotation
      - Bootstrap workflows to ensure new clusters get credentials
      - ClusterHealthReconciler when vault_sync_status is stale
    """
    from app.main import vault_sync_reconciler

    logger.info(
        "Manual vault sync triggered: credential_id=%s, cluster_id=%s",
        body.credential_id,
        body.cluster_id,
    )

    stats = await vault_sync_reconciler.trigger_sync(
        credential_id=body.credential_id,
        cluster_id=body.cluster_id,
    )

    return {
        "status": "ok",
        "sync_stats": stats,
    }


# --- Workspace Reconciliation Manual Trigger ---


@router.post(
    "/workspaces/{workspace_id}/reconcile",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(verify_internal_token)],
)
async def reconcile_workspace(workspace_id: uuid.UUID) -> dict:
    """Manually trigger reconciliation for a specific workspace.

    For Failed workspaces: resets retry count and re-triggers bootstrap.
    For active workspaces with clusters: checks for configuration drift.

    Used by:
      - Admin CLI for manual re-bootstrap after failures
      - Operators investigating drift on a specific workspace
    """
    from app.main import workspace_reconciler

    logger.info(
        "Manual workspace reconciliation triggered: workspace_id=%s", workspace_id
    )

    result = await workspace_reconciler.reconcile_workspace(workspace_id)

    return {
        "status": "ok",
        "reconcile_result": result,
    }
