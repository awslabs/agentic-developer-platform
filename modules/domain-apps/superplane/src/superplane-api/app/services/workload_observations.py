"""Read original workload status/logs through the leased management reader."""

import re
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from app.adapters.operation_authority_source import GrantBackedAuthority
from app.config import settings
from app.database import async_session_factory
from app.models.cluster import Cluster
from app.models.observation import ObservationLease
from app.models.workspace import Workspace
from app.services.bootstrap_observation import manager_document
from app.services.deployment_operations import (
    composition,
    intent_for,
    require_target,
    stored_preview,
)
from app.services.leases import _as_utc
from harness_jobs.identity import decode_payload, payload_digest


async def observe(
    request, db, org_id, workspace_id, deployment_id, *, kind, logs=False, pod_uid=None
):
    if logs and not pod_uid:
        raise HTTPException(422, "select an observed Pod UID for logs")
    authority = GrantBackedAuthority(async_session_factory)

    async def permitted():
        principal = await authority.resolve(
            org_id=str(org_id),
            workspace_id=str(workspace_id),
            permission="workspace:read",
        )
        if principal is None:
            raise HTTPException(403, "workload observation access refused")
        return principal

    await permitted()
    intent = await intent_for(
        db, org_id, workspace_id, deployment_id, workload_kind=kind
    )
    workspace = await db.get(Workspace, workspace_id, populate_existing=True)
    cluster = await db.get(Cluster, intent.cluster_id, populate_existing=True)
    if (
        workspace is None
        or cluster is None
        or workspace.org_id != org_id
        or cluster.org_id != org_id
        or workspace.status == "Deleted"
    ):
        raise HTTPException(404, "workload observation unavailable")
    require_target(intent, workspace, cluster)
    original = stored_preview(intent)
    owner = composition(request)
    async with owner.operation_connect() as connection:
        source = await connection.fetchrow(
            "SELECT r.operation_id,r.allocation_id,r.plan_digest,o.request_payload "
            "FROM controller_deployment_operations r JOIN harness_operations o "
            "ON o.operation_id=r.operation_id AND o.org_id=r.org_id AND o.workspace_id=r.workspace_id "
            "AND o.plan_digest=r.plan_digest WHERE r.deployment_id=$1 AND r.org_id=$2 "
            "AND r.workspace_id=$3 AND r.action='provision'",
            str(deployment_id),
            str(org_id),
            str(workspace_id),
        )
        if (
            source is None
            or source["plan_digest"] != payload_digest(original.request)
            or decode_payload(source["request_payload"]) != original.request
            or source["allocation_id"] != original.request.parameters["allocation_id"]
        ):
            raise HTTPException(503, "original workload operation is unavailable")
        resource_kind = "Job" if kind == "batch" else "Deployment"
        prefix = f"kubernetes:{resource_kind}:{intent.namespace}:{intent.name}:"
        references = await connection.fetch(
            "SELECT provider_reference FROM harness_allocation_resource WHERE org_id=$1 "
            "AND workspace_id=$2 AND operation_id=$3 AND allocation_id=$4 AND provider='aws'",
            str(org_id),
            str(workspace_id),
            source["operation_id"],
            source["allocation_id"],
        )
        uids = {
            row["provider_reference"][len(prefix) :]
            for row in references
            if row["provider_reference"].startswith(prefix)
        }
    if len(uids) != 1:
        raise HTTPException(503, "original workload UID is not recorded")
    uid = next(iter(uids))
    params = {
        "workspace_id": str(workspace_id),
        "deployment_id": str(deployment_id),
        "operation_id": source["operation_id"],
        "kind": kind,
        "name": intent.name,
        "uid": uid,
        "plan_digest": source["plan_digest"],
        "image": original.deployment_target["controller_plan"]["workload"]["image"],
        "logs": "true" if logs else "false",
        "pod_uid": pod_uid or "",
    }
    result = await manager_document("/workload-observation", params)
    await permitted()
    await db.refresh(workspace)
    await db.refresh(cluster)
    if workspace.status == "Deleted" or workspace.cluster_id != cluster.id:
        raise HTTPException(503, "workload target changed during observation")
    require_target(intent, workspace, cluster)
    expected = {
        key: params[key]
        for key in (
            "workspace_id",
            "deployment_id",
            "operation_id",
            "kind",
            "uid",
            "plan_digest",
        )
    }
    expected.update(
        org_id=str(org_id), cluster_id=str(cluster.id), namespace=intent.namespace
    )
    try:
        checked = datetime.fromisoformat(result["checked_at"].replace("Z", "+00:00"))
        expiry = datetime.fromisoformat(
            result["lease_expires_at"].replace("Z", "+00:00")
        )
        instance = str(uuid.UUID(result["instance_id"]))
        now = datetime.now(UTC)
        if (
            any(result.get(key) != value for key, value in expected.items())
            or not now - timedelta(seconds=30) <= checked <= now < expiry
        ):
            raise ValueError("observation binding or freshness changed")
        lease = await db.get(
            ObservationLease, f"controller_management/{org_id}", populate_existing=True
        )
        if (
            lease is None
            or not settings.controller_observation_submitter_id
            or lease.holder
            != settings.controller_observation_submitter_id + ":" + instance
            or type(result["fence_token"]) is not int
            or lease.fence_token != result["fence_token"]
            or _as_utc(lease.expires_at) != expiry
            or expiry <= datetime.now(UTC)
        ):
            raise ValueError("observation lease changed")
        if (
            result["state"]
            not in {
                "unknown",
                "pending",
                "running",
                "succeeded",
                "failed",
                "progressing",
                "ready",
            }
            or not isinstance(result["pods"], list)
            or len(result["pods"]) > 32
        ):
            raise ValueError("invalid progress")
        pods = []
        for pod in result["pods"]:
            if (
                not isinstance(pod, dict)
                or not isinstance(pod["uid"], str)
                or re.fullmatch(r"[a-zA-Z0-9-]{1,255}", pod["uid"]) is None
                or pod["uid"] in {item["uid"] for item in pods}
                or pod["phase"]
                not in {"Pending", "Running", "Succeeded", "Failed", "Unknown", ""}
                or type(pod["ready"]) is not bool
                or type(pod["restarts"]) is not int
                or not 0 <= pod["restarts"] <= 2**31 - 1
                or (
                    pod["exit_code"] is not None
                    and (
                        type(pod["exit_code"]) is not int
                        or not -(2**31) <= pod["exit_code"] < 2**31
                    )
                )
            ):
                raise ValueError("invalid pod progress")
            pods.append(
                {
                    key: pod[key]
                    for key in ("uid", "phase", "ready", "restarts", "exit_code")
                }
            )
        if (
            type(result["logs_truncated"]) is not bool
            or (
                logs
                and (
                    not isinstance(result["logs"], str)
                    or len(result["logs"].encode()) > 65536
                    or result["logs_pod_uid"] != pod_uid
                    or pod_uid not in {p["uid"] for p in pods}
                )
            )
            or (not logs and result["logs"] is not None)
        ):
            raise ValueError("invalid log projection")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HTTPException(503, "workload observation could not be verified") from None
    # Only named fields cross the API boundary. Metadata, container environment,
    # provider errors, credentials and arbitrary result links are never forwarded.
    return {
        **expected,
        "state": result["state"],
        "pods": pods,
        "checked_at": checked,
        "logs": result["logs"] if logs else None,
        "logs_pod_uid": pod_uid if logs else None,
        "logs_truncated": result["logs_truncated"],
        "observed_cost_micros": None,
        "cleanup_status": "unconfirmed",
    }
