"""Authenticated, leased reads of durable workspace registrations.

The controller receives metadata only. This grant cannot deliver credentials,
create registrations, change readiness, or authorize provider work.
"""

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from superplane_contracts import Submitter

from app.database import get_session
from app.models.cluster import Cluster
from app.models.organization import Organization
from app.models.workspace import Workspace
from app.routers.heartbeat import _authenticated_submitter
from app.services import leases

router = APIRouter(prefix="/internal/controller", tags=["controller-management"])


class ReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org_id: uuid.UUID
    instance_id: uuid.UUID


@router.post("/reconcile")
async def reconcile(
    body: ReconcileRequest,
    submitter: Submitter = Depends(_authenticated_submitter),
    db: AsyncSession = Depends(get_session),
):
    scope = f"controller_management/{body.org_id}"
    if scope not in submitter.lease_scopes:
        raise HTTPException(403, "Controller management grant required")
    org = await db.get(Organization, body.org_id)
    if org is None or not org.adp_org_id:
        raise HTTPException(403, "Bound organization required")
    # Serialize first acquisition too: row locking alone cannot lock an absent
    # lease. This is a short database transaction, with no provider or secret I/O.
    if db.bind.dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
            {"scope": scope},
        )
    try:
        lease = await leases.acquire(
            db, submitter=submitter, scope=scope,
            instance_id=str(body.instance_id), duration=timedelta(seconds=45),
        )
    except leases.LeaseUnavailable:
        raise HTTPException(409, "Controller management lease is held") from None
    # Join on both identities. An inconsistent cross-org cluster link must never
    # expose another organization's target, even if old database rows exist.
    rows = (await db.execute(
        select(Workspace, Cluster)
        .outerjoin(Cluster, (Workspace.cluster_id == Cluster.id) & (Cluster.org_id == body.org_id))
        .where(Workspace.org_id == body.org_id)
        .order_by(Workspace.id)
    )).all()
    targets = []
    for workspace, cluster in rows:
        targets.append({
            "workspace_id": str(workspace.id),
            "cluster_id": str(cluster.id) if cluster else None,
            "namespace": workspace.namespace_name,
            "workspace_status": workspace.status,
            "cluster_status": cluster.status if cluster else None,
            "cluster_arn": cluster.eks_cluster_arn if cluster else None,
            "endpoint": cluster.endpoint if cluster else None,
        })
    return {
        "version": 1, "org_id": str(body.org_id),
        "lease_expires_at": lease.expires_at, "fence_token": lease.fence_token,
        "targets": targets,
        "governed_provisioning": False,
    }
