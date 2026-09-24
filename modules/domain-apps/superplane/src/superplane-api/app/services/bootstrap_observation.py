"""Project an active bootstrap claim for scoped reads before registration.

No canonical workspace readiness is written here. The current reservation,
component journal and shared execution lease must agree on the exact operation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from urllib.parse import urlsplit
import uuid

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.models.bootstrap import (
    WorkspaceBootstrapAuthority,
    WorkspaceBootstrapReservation,
)
from app.models.cluster import Cluster
from app.models.observation import ObservationLease
from app.models.workspace import Workspace
from app.services.leases import _as_utc


def operation_connect(request):
    try:
        return request.app.state.trust_composition.operation_connect
    except (AttributeError, RuntimeError) as exc:
        raise HTTPException(503, "Bootstrap operation store is unavailable") from exc


async def live_operation(connect, operation_id, org_id, workspace_id):
    async with connect() as connection:
        return bool(
            await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM harness_operations o "
                "JOIN harness_operation_leases l USING (operation_id) "
                "WHERE o.operation_id=$1 AND o.org_id=$2 AND o.workspace_id=$3 "
                "AND o.action='provision' AND o.cancel_requested_at IS NULL "
                "AND o.state IN ('pending','running','retrying') "
                "AND l.org_id=o.org_id AND l.workspace_id=o.workspace_id "
                "AND l.holder IS NOT NULL AND l.closed_at IS NULL "
                "AND l.expires_at>clock_timestamp() AND l.runtime_deadline>clock_timestamp())",
                operation_id,
                org_id,
                workspace_id,
            )
        )


async def provisional_targets(db, *, org, connect):
    # Bootstrap journals are PostgreSQL-owned. SQLite unit tests intentionally
    # omit these tables; no production backend can silently skip the journal.
    if db.bind.dialect.name != "postgresql":
        return []
    from superplane_bootstrap.state import claim_fingerprint

    aliases = {str(org.id), org.adp_org_id}
    rows = (
        await db.execute(
            select(WorkspaceBootstrapReservation, WorkspaceBootstrapAuthority)
            .join(
                WorkspaceBootstrapAuthority,
                WorkspaceBootstrapAuthority.workspace_id
                == WorkspaceBootstrapReservation.workspace_id,
            )
            .where(
                WorkspaceBootstrapReservation.state == "reserved",
                WorkspaceBootstrapReservation.org_id.in_(aliases),
                WorkspaceBootstrapAuthority.org_id.in_(aliases),
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    targets = {}
    for reservation, authority in rows:
        try:
            identity = json.loads(reservation.identity_json)
            progress = json.loads(authority.progress_json)
            observed = progress["management_observation"]
            claim = claim_fingerprint(reservation.attempt_token)
            workspace_id = str(uuid.UUID(reservation.workspace_id))
            required = {
                "workspace_id": workspace_id,
                "org_id": authority.org_id,
                "operation_id": authority.operation_id,
                "registration_claim": claim,
                "cluster_arn": authority.cluster_arn,
                "namespace": identity["namespace"],
            }
            if (
                authority.claim != claim
                or identity.get("org_id") != authority.org_id
                or identity.get("cluster_arn") != authority.cluster_arn
                or identity.get("workspace_id") != workspace_id
                or any(observed.get(key) != value for key, value in required.items())
                or progress.get("component_inventory_complete") is not True
                or progress.get("phase") not in {"active", "revoking", "revoked"}
                or (
                    progress.get("phase") == "revoked"
                    and progress.get("retain_workspace") is not True
                )
            ):
                continue
            endpoint = urlsplit(observed["endpoint"])
            if (
                endpoint.scheme != "https"
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
                or endpoint.path not in {"", "/"}
            ):
                continue
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        if not await live_operation(
            connect, authority.operation_id, str(org.id), workspace_id
        ):
            continue
        workspace = await db.get(
            Workspace, uuid.UUID(workspace_id), populate_existing=True
        )
        if workspace is not None and (
            workspace.org_id != org.id
            or workspace.status in {"active", "Ready", "Teardown", "Deleted", "retired"}
        ):
            continue
        cluster = await db.scalar(
            select(Cluster).where(Cluster.eks_cluster_arn == authority.cluster_arn)
        )
        if cluster is not None and (
            cluster.org_id != org.id
            or (
                cluster.workspace_id is not None
                and str(cluster.workspace_id) != workspace_id
            )
        ):
            continue
        if workspace_id in targets:
            raise HTTPException(409, "Bootstrap claim is ambiguous")
        targets[workspace_id] = {
            "workspace_id": workspace_id,
            "cluster_id": str(cluster.id) if cluster is not None else "",
            "namespace": identity["namespace"],
            "workspace_status": "Provisioning",
            "cluster_status": "Provisioning",
            "cluster_arn": authority.cluster_arn,
            "endpoint": observed["endpoint"],
            "bootstrap_operation_id": authority.operation_id,
            "bootstrap_org_id": authority.org_id,
            "registration_claim": claim,
            "provisional_observation": True,
        }
    return list(targets.values())


async def manager_snapshot():
    url = urlsplit(settings.controller_status_url)
    if (
        not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in {"", "/"}
        or not (
            url.scheme == "https"
            or (
                url.scheme == "http"
                and url.hostname.endswith((".svc", ".svc.cluster.local"))
            )
        )
        or not settings.controller_registry_credential
    ):
        raise HTTPException(503, "Controller observation transport is unconfigured")
    try:
        async with httpx.AsyncClient(
            timeout=5, trust_env=False, follow_redirects=False
        ) as client:
            async with client.stream(
                "GET",
                settings.controller_status_url.rstrip("/") + "/statusz",
                headers={"Authorization": settings.controller_registry_credential},
            ) as response:
                response.raise_for_status()
                data = bytearray()
                async for part in response.aiter_bytes():
                    data.extend(part)
                    if len(data) > 1 << 20:
                        raise ValueError("oversized manager snapshot")
                snapshot = json.loads(data)
                if not isinstance(snapshot, dict):
                    raise ValueError("invalid manager snapshot")
                return snapshot
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "Controller observation is unavailable") from exc


async def verified_observation(db, *, org, target, snapshot):
    """Check the current manager fence and the exact target it actually inspected."""
    now = datetime.now(UTC)
    try:
        reconciled = datetime.fromisoformat(
            snapshot["last_reconciled"].replace("Z", "+00:00")
        )
        expiry = datetime.fromisoformat(
            snapshot["lease_expires_at"].replace("Z", "+00:00")
        )
        instance = str(uuid.UUID(snapshot["instance_id"]))
        binding = snapshot["bootstrap_observations"][target["workspace_id"]]
        expected = {
            key: target[key]
            for key in (
                "bootstrap_operation_id",
                "registration_claim",
                "cluster_arn",
                "namespace",
            )
        }
        if (
            snapshot.get("mode") != "management"
            or snapshot.get("registry_ready") is not True
            or reconciled.tzinfo is None
            or expiry.tzinfo is None
            or not now - timedelta(seconds=30) <= reconciled <= now < expiry
            or binding != expected
            or snapshot["targets"][target["workspace_id"]]
            != "observed_execution_unavailable"
        ):
            raise ValueError("unverified snapshot")
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise HTTPException(
            409, "Bootstrap target has not been freshly observed"
        ) from exc
    lease = await db.get(
        ObservationLease, f"controller_management/{org.id}", populate_existing=True
    )
    if (
        lease is None
        or not settings.controller_observation_submitter_id
        or lease.holder != settings.controller_observation_submitter_id + ":" + instance
        or type(snapshot.get("fence_token")) is not int
        or lease.fence_token != snapshot["fence_token"]
        or _as_utc(lease.expires_at) != expiry
        or expiry <= datetime.now(UTC)
    ):
        raise HTTPException(409, "Controller observation lease is stale")
    return {
        "workspace_id": target["workspace_id"],
        "org_id": target["bootstrap_org_id"],
        "operation_id": target["bootstrap_operation_id"],
        "cluster_arn": target["cluster_arn"],
        "namespace": target["namespace"],
        "registration_claim": target["registration_claim"],
        "registry_ready": True,
        "last_reconciled": reconciled,
        "lease_expires_at": expiry,
        "target_status": "observed_execution_unavailable",
    }
