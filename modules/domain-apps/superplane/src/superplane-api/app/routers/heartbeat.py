"""Heartbeat and observation ingestion for data plane reporters.

Routes:
    POST /internal/heartbeat                — legacy shared-token path, being retired
    POST /internal/observations             — observation contract v1 (authenticated)
    POST /internal/observations/leases         — acquire/renew a lease
    POST /internal/observations/leases/release — release a lease
    GET  /internal/observations/{cluster}      — scoped read of recorded observations

## Why two write paths exist at once

The legacy heartbeat has only the coarse shared internal token — no submitter
identity or workspace-bound signature. U15 closes that authorization gap, and it
cannot delete the route in the same change that adds its replacement: the receiver
has to be deployable *before* any sender changes, so continuity can be demonstrated
across the cutover rather than asserted.

So `settings.legacy_heartbeat_enabled` gates it. It defaults true (deploy the
receiver, nothing breaks), and the cutover step sets it false, after which the
route refuses with 410 and the authenticated contract is the only write path. The
runbook at `docs/runbooks/superplane-monitor-grant-withdrawal.md` sequences that
against the database grant withdrawal.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.models.cluster import Cluster
from app.models.event import Event
from app.models.observation import ObservationReceipt
from app.routers.internal import verify_internal_token
from app.schemas.proxy import HeartbeatRequest, HeartbeatResponse
from app.services import leases as lease_service
from superplane_contracts.auth import MAX_OBSERVATION_BODY_BYTES
from app.services import observations as observation_service

from superplane_contracts import (
    DEFAULT_LEASE_DURATION,
    ContractViolation,
    Submitter,
    authorize_read,
)

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

    Retained behind the shared internal token only for continuity across the
    cutover; see the module docstring. `POST /internal/observations` is the
    supported workspace-scoped path.
    """
    if not settings.legacy_heartbeat_enabled:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=(
                "The legacy heartbeat endpoint has been retired. "
                "Submit observations to POST /internal/observations."
            ),
        )
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


# --- observation contract v1 (issue #5056, U15) ------------------------------


class ObservationAccepted(BaseModel):
    """Receipt for an accepted submission.

    `applied` is false for an idempotent retry — the submission was valid and the
    recorded state already reflects it. Distinguished from a fresh accept so a
    sender can tell "my retry landed" from "I wrote twice".
    """

    accepted: bool = True
    applied: bool
    cluster_id: str
    status: str


class LeaseAcquireRequest(BaseModel):
    """A lease acquire/renew request.

    There is no `holder` field on purpose: the holder is derived from the
    authenticated submitter, so a caller cannot name itself as somebody else and
    release that party's lease. `instance_id` only distinguishes replicas *within*
    an authenticated submitter.
    """

    resource_type: str = Field(min_length=1, max_length=100)
    resource_id: str = Field(min_length=1, max_length=200)
    instance_id: str = Field(default="default", max_length=200)
    duration_seconds: int = Field(default=int(DEFAULT_LEASE_DURATION.total_seconds()))


class LeaseReleaseRequest(BaseModel):
    resource_type: str = Field(min_length=1, max_length=100)
    resource_id: str = Field(min_length=1, max_length=200)
    instance_id: str = Field(default="default", max_length=200)
    fence_token: int


class LeaseGranted(BaseModel):
    scope: str
    expires_at: datetime
    fence_token: int


async def _authenticated_submitter(request: Request):
    """Resolve the caller's credential to an authenticated submitter.

    Used by the lease and read routes. The submit route does not use this: it must
    hand the *raw request bytes* to `verify_submission`, which authenticates and
    verifies the signature in one step, and splitting that would mean
    authenticating a body separately from the bytes that were signed.
    """
    credential = (request.headers.get("authorization") or "").strip()
    resolver = observation_service.load_submitters()
    submitter = resolver.resolve(credential) if credential else None
    if submitter is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthenticated: credential not accepted",
        )
    return submitter


@router.post(
    "/observations",
    response_model=ObservationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_observation(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> ObservationAccepted:
    """Accept a signed, workspace-scoped observation.

    Takes the raw request body rather than a parsed model, because the signature
    covers the exact transmitted UTF-8 bytes. Letting FastAPI parse and re-encode
    the body first would verify a signature against bytes the sender never sent —
    Go and Python differ on number formatting, key order and escaping — so the
    body is read with `await request.body()` and passed through unmodified.
    """
    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > MAX_OBSERVATION_BODY_BYTES:
            raise HTTPException(status_code=413, detail="observation body too large")
        chunks.extend(chunk)
    body = bytes(chunks)
    try:
        observation, applied = await observation_service.record_observation(
            db, body=body, headers=dict(request.headers)
        )
    except observation_service.ObservationRefused as refusal:
        # `refusal.reason` comes from the contract or the receiver's own
        # non-enumerating constants; neither echoes a credential or a signature.
        raise HTTPException(
            status_code=refusal.status_code, detail=refusal.reason
        ) from None

    return ObservationAccepted(
        applied=applied,
        cluster_id=observation.subject.cluster_id,
        status=observation.status.value,
    )


@router.post(
    "/observations/leases",
    response_model=LeaseGranted,
    status_code=status.HTTP_200_OK,
)
async def acquire_lease(
    body: LeaseAcquireRequest,
    submitter: Submitter = Depends(_authenticated_submitter),
    db: AsyncSession = Depends(get_session),
) -> LeaseGranted:
    """Acquire or renew a lease, replacing the monitor's `reconcile_locks` writes."""
    try:
        resource_id = await observation_service.authorize_lease_scope(
            db, submitter, body.resource_type, body.resource_id
        )
    except observation_service.ObservationRefused as refusal:
        raise HTTPException(
            status_code=refusal.status_code, detail=refusal.reason
        ) from None
    scope = lease_service.scope_for(body.resource_type, resource_id)
    try:
        granted = await lease_service.acquire(
            db,
            submitter=submitter,
            scope=scope,
            instance_id=body.instance_id,
            duration=timedelta(seconds=body.duration_seconds),
        )
    except ContractViolation as violation:
        # The contract's own validation: a duration above the 15-minute ceiling,
        # or a blank scope. A client error, not a conflict.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(violation)
        ) from None
    except lease_service.LeaseUnavailable as unavailable:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=unavailable.reason
        ) from None

    return LeaseGranted(
        scope=granted.scope,
        expires_at=granted.expires_at,
        fence_token=granted.fence_token,
    )


@router.post("/observations/leases/release")
async def release_lease(
    body: LeaseReleaseRequest,
    submitter: Submitter = Depends(_authenticated_submitter),
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    """Release a lease held by this authenticated caller."""
    try:
        resource_id = await observation_service.authorize_lease_scope(
            db, submitter, body.resource_type, body.resource_id
        )
    except observation_service.ObservationRefused as refusal:
        raise HTTPException(
            status_code=refusal.status_code, detail=refusal.reason
        ) from None
    scope = lease_service.scope_for(body.resource_type, resource_id)
    try:
        await lease_service.release(
            db,
            submitter=submitter,
            scope=scope,
            instance_id=body.instance_id,
            fence_token=body.fence_token,
        )
    except lease_service.LeaseUnavailable as unavailable:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=unavailable.reason
        ) from None
    return {"released": True}


class ScopedCluster(BaseModel):
    """A cluster the caller may observe, as the monitor needs to see it.

    A deliberately narrow projection of the `clusters` row. The monitor's direct
    `SELECT` returned every column; this returns the fields its health checks
    actually read plus the resolved owning workspace. Withdrawing a table grant
    and then serving the same full row over HTTP would move the read rather than
    reduce it.

    `last_heartbeat` and `last_reconciled_at` are both present and mean different
    things: the first is when the data-plane *controller* last reported in (the
    input to `checkHeartbeatFreshness`), the second is when the *monitor* last
    completed a cycle against this cluster (what a submission records). Reading
    the second is how the cutover's continuity check observes that the monitor is
    alive without the monitor being able to vouch for the controller.
    """

    cluster_id: str
    workspace: str
    org_id: str
    name: str
    status: str
    health_status: str | None
    last_heartbeat: datetime | None
    last_reconciled_at: datetime | None
    actual_state_json: dict | None


class ScopedEventRequest(BaseModel):
    """An event a monitor asks the receiver to record.

    No `org_id` and no `resource_id` beyond the path's cluster: both are taken
    from the authorized cluster row, so a caller cannot file an event against
    another tenant's audit trail.
    """

    event_type: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=2000)
    details: dict = Field(default_factory=dict)


@router.get("/observations/clusters", response_model=list[ScopedCluster])
async def list_observable_clusters(
    submitter: Submitter = Depends(_authenticated_submitter),
    statuses: str = "Active,Running,Provisioning,Pending",
    db: AsyncSession = Depends(get_session),
) -> list[ScopedCluster]:
    """List the clusters this caller may observe.

    Replaces the monitor's `SELECT ... FROM clusters WHERE status IN (...)`. The
    workspace filter is not a parameter: it comes from the authenticated
    submitter's grant, so there is no request a caller can compose that widens it.

    Declared before `/observations/{cluster_id}` because FastAPI matches routes in
    declaration order and `clusters` would otherwise be captured as a cluster id.
    """
    wanted = tuple(s.strip() for s in statuses.split(",") if s.strip())
    if not wanted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="at least one status is required",
        )
    rows = await observation_service.list_scoped_clusters(
        db, submitter, statuses=wanted
    )
    return [
        ScopedCluster(
            cluster_id=str(cluster.id),
            workspace=workspace,
            org_id=str(cluster.org_id),
            name=cluster.name,
            status=cluster.status,
            health_status=cluster.health_status,
            last_heartbeat=cluster.last_heartbeat,
            last_reconciled_at=cluster.last_reconciled_at,
            actual_state_json=cluster.actual_state_json,
        )
        for cluster, workspace in rows
    ]


@router.get("/observations/{cluster_id}/cost-history")
async def read_cost_history(
    cluster_id: uuid.UUID,
    submitter: Submitter = Depends(_authenticated_submitter),
    window_seconds: int = 24 * 60 * 60,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Recent hourly cost values for one authorized cluster.

    Replaces the monitor's query against `events.details_json`. Reading another
    workspace's spend history is a disclosure, so this is scoped exactly as the
    observation read is, with the same indistinguishable 404.
    """
    if window_seconds <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="window_seconds must be positive",
        )
    try:
        cluster, _ = await observation_service.authorized_cluster(
            db, submitter, cluster_id
        )
    except observation_service.ObservationRefused as refusal:
        raise HTTPException(
            status_code=refusal.status_code, detail=refusal.reason
        ) from None

    since = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    costs = await observation_service.cost_history(db, cluster, since=since)
    return {"cluster_id": str(cluster.id), "costs": costs}


@router.post("/observations/{cluster_id}/events", status_code=status.HTTP_201_CREATED)
async def record_cluster_event(
    cluster_id: uuid.UUID,
    body: ScopedEventRequest,
    submitter: Submitter = Depends(_authenticated_submitter),
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    """Record an event about one authorized cluster.

    Replaces the monitor's `INSERT INTO events`. The old call passed `org_id` as
    an argument; here it is read from the authorized cluster row instead, so the
    caller cannot choose whose audit trail it writes to.
    """
    try:
        cluster, _ = await observation_service.authorized_cluster(
            db, submitter, cluster_id
        )
    except observation_service.ObservationRefused as refusal:
        raise HTTPException(
            status_code=refusal.status_code, detail=refusal.reason
        ) from None

    observation_service.record_scoped_event(
        db,
        cluster,
        event_type=body.event_type,
        message=body.message,
        details=body.details,
        submitter_id=submitter.submitter_id,
    )
    await db.commit()
    return {"recorded": True}


@router.get("/observations/{cluster_id}")
async def read_observation(
    cluster_id: uuid.UUID,
    submitter: Submitter = Depends(_authenticated_submitter),
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Read the recorded observation for a cluster, scoped to the caller's grant.

    Read is authorized separately from submit and is equally workspace-bound. A
    fleet observation is an operational map of a tenant's estate — which clusters
    exist, which are unreachable, what they cost — so reading another workspace's
    observations is a disclosure even though nothing is written.

    A cluster outside the caller's scope and a cluster that does not exist return
    the same 404, so this cannot be used to test whether a cluster exists.
    """
    not_found = HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="observation not found"
    )

    receipt = await db.get(ObservationReceipt, cluster_id)
    if receipt is None:
        raise not_found
    if not authorize_read(submitter, receipt.workspace).allowed:
        raise not_found

    return {
        "cluster_id": str(receipt.cluster_id),
        "workspace": receipt.workspace,
        "status": receipt.last_status,
        "reported_at": receipt.last_reported_at.isoformat(),
        "submitter_id": receipt.submitter_id,
    }
