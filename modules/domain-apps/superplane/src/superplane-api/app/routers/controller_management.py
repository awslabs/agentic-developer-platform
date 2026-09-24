"""Authenticated, leased reads of durable workspace registrations.

The controller receives metadata only. This grant cannot deliver credentials,
create registrations, change readiness, or authorize provider work.
"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from superplane_contracts import Submitter

from app.database import get_session
from app.models.cluster import Cluster
from app.models.controller_execution import (
    ControllerExecution,
    ControllerExecutionAccounting,
)
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
    request: Request = None,
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
            db,
            submitter=submitter,
            scope=scope,
            instance_id=str(body.instance_id),
            duration=timedelta(seconds=45),
        )
    except leases.LeaseUnavailable:
        raise HTTPException(409, "Controller management lease is held") from None
    # Join on both identities. An inconsistent cross-org cluster link must never
    # expose another organization's target, even if old database rows exist.
    rows = (await db.execute(registered_targets_query(body.org_id))).mappings().all()
    targets = [
        {
            key: str(value) if isinstance(value, uuid.UUID) else value
            for key, value in row.items()
        }
        for row in rows
    ]
    # This credential can only read expiring metadata for its own replica. The
    # socket token arrives on a separate pod-private mount from the trusted service.
    executions = (
        (
            await db.execute(
                select(ControllerExecution)
                .where(
                    ControllerExecution.org_id == str(body.org_id),
                    ControllerExecution.controller_holder == lease.holder,
                    ControllerExecution.expires_at > datetime.now(UTC),
                )
                .limit(33)
            )
        )
        .scalars()
        .all()
    )
    if len(executions) > 32:
        raise HTTPException(503, "Controller assignment capacity exceeded")
    by_workspace = {}
    for execution in executions:
        assignment = execution.assignment
        if (
            assignment.get("org_id") != str(org.id)
            or assignment.get("workspace_id") != str(execution.workspace_id)
            or assignment.get("operation_id") != execution.operation_id
        ):
            raise HTTPException(503, "Controller assignment binding mismatch")
        by_workspace.setdefault(str(execution.workspace_id), []).append(assignment)
    accounting = (
        (
            await db.execute(
                select(ControllerExecutionAccounting)
                .where(
                    ControllerExecutionAccounting.org_id == str(body.org_id),
                )
                .order_by(
                    ControllerExecutionAccounting.observation["checked_at"].as_string().desc(),
                    ControllerExecutionAccounting.operation_id,
                )
                .limit(256)
            )
        )
        .scalars()
        .all()
    )
    for target in targets:
        target["execution_reports"] = [
            entry.observation
            for entry in accounting
            if str(entry.workspace_id) == target["workspace_id"]
        ]
        target["execution_org_id"] = str(org.id)
        target["execution_assignments"] = by_workspace.get(target["workspace_id"], [])
        target["provider_observations"] = [
            assignment["provider_observation"]
            for assignment in target["execution_assignments"]
            if isinstance(assignment.get("provider_observation"), dict)
        ]
    if request is not None:
        from app.services.bootstrap_observation import (
            operation_connect,
            provisional_targets,
        )

        provisional = await provisional_targets(
            db, org=org, connect=lambda: operation_connect(request)()
        )
        provisional_ids = {target["workspace_id"] for target in provisional}
        targets = [
            target
            for target in targets
            if target["workspace_id"] not in provisional_ids
        ]
        targets.extend(provisional)
    return {
        "version": 1,
        "org_id": str(body.org_id),
        "lease_expires_at": lease.expires_at,
        "fence_token": lease.fence_token,
        "targets": targets,
        "governed_provisioning": bool(executions),
    }


def registered_targets_query(org_id: uuid.UUID):
    """Canonical discovery query, shared with the bootstrap publication regression."""
    return (
        select(
            Workspace.id.label("workspace_id"),
            Cluster.id.label("cluster_id"),
            Workspace.namespace_name.label("namespace"),
            Workspace.status.label("workspace_status"),
            Cluster.status.label("cluster_status"),
            Cluster.eks_cluster_arn.label("cluster_arn"),
            Cluster.endpoint.label("endpoint"),
        )
        .outerjoin(
            Cluster, (Workspace.cluster_id == Cluster.id) & (Cluster.org_id == org_id)
        )
        .where(Workspace.org_id == org_id)
        .order_by(Workspace.id)
    )
