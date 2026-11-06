"""Current native reader identity, without credentials or execution authority.

An installation-scoped renewer uses this metadata before and after provider I/O.
Bootstrap requires the original live lease; registered workspaces instead require
the retained, positively revoked native journal and canonical ownership.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from superplane_contracts import Submitter

from app.database import get_session
from app.models.bootstrap import (
    WorkspaceBootstrapAuthority,
    WorkspaceBootstrapReservation,
)
from app.models.cluster import Cluster
from app.models.workspace import Workspace
from app.routers.heartbeat import _authenticated_submitter
from app.services.bootstrap_observation import live_operation, operation_connect

router = APIRouter(prefix="/internal/readers", tags=["dedicated-readers"])


def native_identity(reservation, authority):
    from superplane_bootstrap.state import claim_fingerprint

    try:
        identity = json.loads(reservation.identity_json)
        progress = json.loads(authority.progress_json)
        claim = claim_fingerprint(reservation.attempt_token)
        namespace = identity["namespace"]
        observed = progress["management_observation"]
        if (
            authority.claim != claim
            or identity.get("membership_generation")
            or identity.get("org_id") != authority.org_id
            or identity.get("cluster_arn") != authority.cluster_arn
            or identity.get("workspace_id") != authority.workspace_id
            or progress.get("component_inventory_complete") is not True
            or progress.get("component_inventory_mode") != "management"
            or progress.get("phase") not in {"active", "revoking", "revoked"}
            or (
                progress["phase"] != "active"
                and progress.get("retain_workspace") is not True
            )
            or any(
                observed.get(k) != v
                for k, v in {
                    "workspace_id": authority.workspace_id,
                    "org_id": authority.org_id,
                    "operation_id": authority.operation_id,
                    "registration_claim": claim,
                    "cluster_arn": authority.cluster_arn,
                    "namespace": namespace,
                }.items()
            )
        ):
            return None
        for key in (
            "reader-issuer-cluster-role",
            "reader-issuer-cluster-binding",
            "reader-issuer-namespace-role",
            "reader-issuer-namespace-binding",
        ):
            if progress.get(key, {}).get("phase") not in {"granted", "adopted"}:
                return None
        key = json.dumps(
            ["ServiceAccount", namespace, "superplane-controller"],
            separators=(",", ":"),
        )
        component = progress["components"][key]
        if component["phase"] not in {"owned", "adopted"}:
            return None
        namespace_uid = progress["workspace-namespace"]["identity"]["uid"]
        service_account_uid = component["identity"]["uid"]
        if not namespace_uid or not service_account_uid:
            return None
        if reservation.state == "registered" and not (
            authority.revoked
            and progress["phase"] == "revoked"
            and progress.get("complete")
            and progress.get("retain_workspace")
        ):
            return None
        return {
            "workspace_id": authority.workspace_id,
            "org_id": authority.org_id,
            "cluster_arn": authority.cluster_arn,
            "endpoint": observed["endpoint"],
            "namespace": namespace,
            "namespace_uid": namespace_uid,
            "service_account": "superplane-controller",
            "service_account_uid": service_account_uid,
            "generation": authority.generation,
            "operation_id": authority.operation_id,
            "registration_claim": claim,
            "registered": reservation.state == "registered",
        }
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


@router.get("/{workspace_id}")
async def reader_target(
    workspace_id: uuid.UUID,
    request: Request,
    submitter: Annotated[Submitter, Depends(_authenticated_submitter)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    workspace = await db.get(Workspace, workspace_id, populate_existing=True)
    if (
        workspace is None
        or f"workspace_reader/{workspace.org_id}/{workspace_id}"
        not in submitter.lease_scopes
    ):
        raise HTTPException(403, "Installed workspace reader grant required")
    result = {"version": 1, "workspace_id": str(workspace_id), "active": False}
    if db.bind.dialect.name != "postgresql":
        raise HTTPException(503, "Native reader journal unavailable")
    if workspace.shared_cluster_id is not None or workspace.status in {
        "Teardown",
        "Deleted",
        "retired",
    }:
        return result
    reservation = await db.get(
        WorkspaceBootstrapReservation, str(workspace_id), populate_existing=True
    )
    if reservation is None:
        return result
    authorities = (
        await db.scalars(
            select(WorkspaceBootstrapAuthority)
            .where(
                WorkspaceBootstrapAuthority.workspace_id == str(workspace_id),
                WorkspaceBootstrapAuthority.org_id == str(workspace.org_id),
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    matching = [
        (authority, native_identity(reservation, authority))
        for authority in authorities
    ]
    matching = [
        (authority, target) for authority, target in matching if target is not None
    ]
    if len(matching) > 1:
        raise HTTPException(409, "Native reader claim is ambiguous")
    if not matching:
        return result
    authority, target = matching[0]
    if target["registered"]:
        cluster = (
            await db.get(Cluster, workspace.cluster_id, populate_existing=True)
            if workspace.cluster_id
            else None
        )
        if (
            cluster is None
            or workspace.status not in {"Ready", "active"}
            or cluster.status not in {"Ready", "Active"}
            or cluster.org_id != workspace.org_id
            or cluster.workspace_id != workspace.id
            or cluster.eks_cluster_arn != target["cluster_arn"]
            or cluster.endpoint != target["endpoint"]
            or workspace.namespace_name != target["namespace"]
        ):
            return result
    elif not await live_operation(
        operation_connect(request),
        authority.operation_id,
        str(workspace.org_id),
        str(workspace_id),
    ):
        return result
    return {
        **result,
        "active": True,
        "target": target,
        "expires_at": datetime.now(UTC) + timedelta(seconds=30),
    }
