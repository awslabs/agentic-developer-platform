"""Read-only lifecycle snapshots and revision fences for the installed ADP CLI."""

import hashlib
import json
from datetime import timezone

from fastapi import HTTPException
from sqlalchemy import select

from app.models.deployment import Deployment
from app.models.cluster import Cluster
from app.models.provider_connection import ProviderConnectionBinding
from app.models.provider_handle import ProviderOperation
from app.models.workspace import Workspace


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def timestamp(value):
    if value is None:
        return None
    return (
        value.astimezone(timezone.utc)
        if value.tzinfo
        else value.replace(tzinfo=timezone.utc)
    ).isoformat()


async def workspace_snapshot(db, org_id, workspace_id, *, lock=False):
    statement = select(Workspace).where(
        Workspace.id == workspace_id, Workspace.org_id == org_id
    )
    if lock:
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    workspace = (await db.execute(statement)).scalar_one_or_none()
    if workspace is None:
        raise HTTPException(404, "Workspace not found")
    deployments = (
        (
            await db.execute(
                select(Deployment)
                .where(
                    Deployment.org_id == org_id, Deployment.workspace_id == workspace_id
                )
                .order_by(Deployment.id)
                .limit(101)
            )
        )
        .scalars()
        .all()
    )
    bindings = (
        (
            await db.execute(
                select(ProviderConnectionBinding)
                .where(ProviderConnectionBinding.workspace_id == workspace_id)
                .order_by(ProviderConnectionBinding.id)
                .limit(101)
            )
        )
        .scalars()
        .all()
    )
    handles = (
        (
            await db.execute(
                select(ProviderOperation)
                .where(
                    ProviderOperation.org_id == str(org_id),
                    ProviderOperation.workspace == str(workspace_id),
                )
                .order_by(ProviderOperation.idempotency_key)
                .limit(101)
            )
        )
        .scalars()
        .all()
    )
    if len(deployments) > 100 or len(bindings) > 100 or len(handles) > 100:
        raise HTTPException(
            409,
            "Lifecycle inventory exceeds bounded review; use managed retirement review",
        )
    cluster = await db.scalar(select(Cluster).where(Cluster.id == workspace.cluster_id, Cluster.org_id == org_id)) if workspace.cluster_id else None
    cluster_arn = cluster.eks_cluster_arn if cluster else None
    home_region = cluster_arn.split(":")[3] if cluster_arn and cluster_arn.startswith("arn:") and len(cluster_arn.split(":")) >= 6 else None
    result = {
        "workspace_id": str(workspace_id),
        "org_id": str(org_id),
        "name": workspace.name,
        "status": workspace.status,
        "isolation_mode": workspace.isolation_mode,
        "cluster_id": str(workspace.cluster_id) if workspace.cluster_id else None,
        "shared_cluster_id": str(workspace.shared_cluster_id) if workspace.shared_cluster_id else None,
        "namespace": workspace.namespace_name,
        "cluster_provider": cluster.cloud_provider if cluster else None,
        "cluster_home_region": home_region,
        "compute_provider_region": "Selected independently by each approved allocation profile; not inferred from cluster proximity",
        "is_default": workspace.is_default,
        "updated_at": timestamp(workspace.updated_at),
        "provisioning_operation_id": workspace.provisioning_operation_id,
        "teardown_operation_id": workspace.teardown_operation_id,
        "deployments": [
            {"id": str(row.id), "name": row.name, "status": row.status}
            for row in deployments
        ],
        "provider_connections": [str(row.connection_id) for row in bindings],
        "provider_handles": [
            {
                "operation_key": row.idempotency_key,
                "provider": row.provider,
                "resource_name": row.resource_name,
                "state": row.state,
                "provider_presence": row.provider_presence,
            }
            for row in handles
        ],
        "billing_state": "unconfirmed",
        "provider_resource_observation": "not_probed",
        "limitation": "A metadata transition or delete acknowledgement does not prove provider resource deletion or stopped billing.",
    }
    result["revision"] = digest(result)
    return workspace, result


def connection_revision(connection, binding):
    return digest(
        {
            "id": str(connection.id),
            "workspace": str(binding.workspace_id),
            "credential": connection.adp_credential_id,
            "binding_credential": binding.adp_credential_id,
            "status": connection.status,
            "updated_at": timestamp(connection.updated_at),
            "validated_at": timestamp(connection.validated_at),
            "bound_at": timestamp(binding.bound_at),
        }
    )


def require_connection_revision(expected, connection, binding):
    if expected is not None and expected != connection_revision(connection, binding):
        raise HTTPException(
            409, "Provider connection changed; review its current revision"
        )
