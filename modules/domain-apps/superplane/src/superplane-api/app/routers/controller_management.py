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
from app.models.cluster_membership import ClusterMembership
from app.models.membership_credential import MembershipCredential
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
                    ControllerExecutionAccounting.observation["checked_at"]
                    .as_string()
                    .desc(),
                    ControllerExecutionAccounting.operation_id,
                )
                .limit(256)
            )
        )
        .scalars()
        .all()
    )
    for target in targets:
        shared_cluster_id = target.pop("shared_cluster_id", None)
        if shared_cluster_id is not None:
            target["shared_membership"] = True
            binding = await reader_membership(db, org.id, target["workspace_id"])
            if binding is not None:
                target.update(binding)
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
            Workspace.shared_cluster_id.label("shared_cluster_id"),
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


async def reader_membership(db, org_id, workspace_id, *, provisional_identity=None):
    """Current public reader identity, never credential material or authority.

    Projected revisions are visible only to the original live bootstrap claim caller
    in bootstrap_observation; ordinary reconciliation receives only active revisions.
    """
    provisional = provisional_identity is not None
    query = (
        select(ClusterMembership, MembershipCredential, Cluster)
        .join(
            MembershipCredential,
            MembershipCredential.membership_id == ClusterMembership.id,
        )
        .join(
            Cluster,
            (Cluster.id == ClusterMembership.cluster_id)
            & (Cluster.org_id == ClusterMembership.org_id),
        )
        .join(
            Workspace,
            (Workspace.id == ClusterMembership.workspace_id)
            & (Workspace.org_id == ClusterMembership.org_id),
        )
        .where(
            ClusterMembership.org_id == org_id,
            ClusterMembership.workspace_id == uuid.UUID(str(workspace_id)),
            Workspace.cluster_id == ClusterMembership.cluster_id,
            Workspace.shared_cluster_id == ClusterMembership.cluster_id,
            Workspace.namespace_name == ClusterMembership.namespace,
            Cluster.sharing_enabled.is_(True),
            Cluster.status.in_(["Ready", "Active"]),
            ClusterMembership.state.in_(
                ["reserved", "active"] if provisional else ["active"]
            ),
            MembershipCredential.scope == "reader",
            MembershipCredential.state == ("projected" if provisional else "active"),
            MembershipCredential.expires_at > datetime.now(UTC),
        )
        .execution_options(populate_existing=True)
    )
    rows = (await db.execute(query)).all()
    if len(rows) != 1:
        return None
    member, credential, cluster = rows[0]
    if provisional:
        if (
            provisional_identity.get("membership_generation") != member.generation
            or provisional_identity.get("membership_request_id")
            != str(member.operation_id)
            or provisional_identity.get("membership_cluster_id")
            != str(member.cluster_id)
            or provisional_identity.get("namespace_uid")
            not in {None, "", credential.namespace_uid}
            or provisional_identity.get("cluster_arn") != cluster.eks_cluster_arn
            or provisional_identity.get("namespace") != member.namespace
            or member.namespace_uid not in {None, credential.namespace_uid}
        ):
            return None
    elif member.namespace_uid != credential.namespace_uid:
        return None
    from app.services.leases import _as_utc

    if not credential.service_account_uid or not credential.namespace_uid:
        return None
    return {
        "shared_membership": True,
        "platform_eligible": cluster.platform_eligible is True,
        "membership_credential": {
            "org_id": str(member.org_id),
            "workspace_id": str(member.workspace_id),
            "cluster_id": str(member.cluster_id),
            "cluster_arn": cluster.eks_cluster_arn,
            "generation": member.generation,
            "namespace": member.namespace,
            "namespace_uid": credential.namespace_uid,
            "service_account_uid": credential.service_account_uid,
            "revision": credential.revision,
            "expires_at": _as_utc(credential.expires_at).isoformat(),
            "scope": "reader",
        },
    }
