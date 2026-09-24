"""Quota enforcement service — validates resource requests against org and workspace quotas.

Quotas are stored as JSON in the quotas_json column on both organizations and workspaces.
This service parses those JSON blobs and enforces limits before resource creation.

Enforcement points:
1. Workspace creation  -> check org max_workspaces
2. Node provisioning   -> check workspace max_nodes, max_gpus, allowed_clouds
3. Deployment creation -> check workspace max_gpus (gpu_per_replica * replicas)
4. Budget (daily cost) -> checked by CostReconciler, but also pre-checked here
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.middleware.quota import QUOTA_DECISION_ATTR
from app.models.deployment import Deployment
from app.models.node import Node
from app.models.organization import Organization
from app.models.workspace import Workspace

logger = logging.getLogger(__name__)

# Deployment statuses that hold no capacity, so do not count against a workspace's
# committed GPUs. Everything else — including in-flight creation — does count: a
# reservation that stops counting the moment it is uncertain is how two concurrent
# requests both see the same headroom.
RELEASED_DEPLOYMENT_STATUSES = ("Deleted", "Failed")

# Statuses a deployment row can hold while its cluster write is still in flight.
# `reserve_deployment_gpus` writes this, and it counts toward the workspace total.
DEPLOYMENT_STATUS_RESERVED = "Pending"

# Default plan-based quotas (fallback when no explicit quota is set)
PLAN_DEFAULTS: dict[str, dict[str, Any]] = {
    "free": {
        "max_workspaces": 1,
        "max_nodes": 2,
        "max_gpus": 4,
        "max_cost_per_day": 50,
    },
    "pro": {
        "max_workspaces": 5,
        "max_nodes": 20,
        "max_gpus": 32,
        "max_cost_per_day": 1000,
    },
    "enterprise": {
        "max_workspaces": 50,
        "max_nodes": 200,
        "max_gpus": 256,
        "max_cost_per_day": 10000,
    },
}


def parse_quotas(quotas_json: str | None, plan: str = "free") -> dict[str, Any]:
    """Parse quotas from JSON string, falling back to plan defaults."""
    if quotas_json:
        try:
            return json.loads(quotas_json)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Invalid quotas_json, using plan defaults: %s", quotas_json)
    return PLAN_DEFAULTS.get(plan, PLAN_DEFAULTS["free"]).copy()


def merge_quotas(existing: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """Merge quota updates into existing quotas. Only non-None values are updated."""
    merged = existing.copy()
    for key, value in updates.items():
        if value is not None:
            merged[key] = value
    return merged


async def get_org_quotas(
    org_id: uuid.UUID, db: AsyncSession
) -> tuple[Organization, dict[str, Any]]:
    """Load org and its effective quotas.

    Returns:
        Tuple of (Organization, quota dict).

    Raises:
        HTTPException 404 if org not found.
    """
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found"
        )

    # Persisted quotas are overrides. Empty/partial records must not erase a
    # plan's GPU or spend ceiling; null also means inherit, as on updates.
    quotas = merge_quotas(
        parse_quotas(None, org.billing_plan),
        parse_quotas(org.quotas_json, org.billing_plan),
    )
    return org, quotas


async def get_workspace_quotas(
    workspace_id: uuid.UUID,
    org_id: uuid.UUID,
    db: AsyncSession,
) -> tuple[Workspace, dict[str, Any]]:
    """Load workspace and its effective quotas.

    Workspace quotas inherit from org quotas, with workspace-level overrides.

    Returns:
        Tuple of (Workspace, quota dict).

    Raises:
        HTTPException 404 if workspace not found.
    """
    result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id, Workspace.org_id == org_id
        )
    )
    workspace = result.scalar_one_or_none()
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found"
        )

    # Start with org-level quotas
    _, org_quotas = await get_org_quotas(org_id, db)

    # Overlay only the workspace's OWN explicit overrides.
    #
    # This used to call `parse_quotas(workspace.quotas_json, "free")`, which falls back
    # to the FREE plan's defaults whenever a workspace records no overrides of its own —
    # and those defaults then overlaid the org's quotas. So every workspace without an
    # explicit `quotas_json` silently capped its enterprise org at the free plan's 4
    # GPUs, and the inheritance this function documents ran backwards (issue #5671, A15).
    #
    # Found by the A15 regression test for "a workspace with no recorded budget falls
    # back to the plan default": the workspace inherited 4 instead of its org's 256. The
    # failure mode is over-strict rather than permissive, so it refuses legitimate
    # requests instead of admitting over-budget ones — but a quota that refuses at 1/64th
    # of the entitlement is not the enforcement this issue is asked to deliver, and it
    # would be read as the new check being broken.
    #
    # Absent overrides now mean "inherit", which is what the docstring says. A workspace
    # that wants a tighter ceiling states it, via `quotas_json` or the `budget_*` columns
    # applied below.
    ws_quotas = parse_quotas(workspace.quotas_json) if workspace.quotas_json else {}
    effective = org_quotas.copy()
    for key, val in ws_quotas.items():
        if val is not None:
            effective[key] = val

    # Also use budget fields from workspace model directly
    if workspace.budget_max_daily_usd is not None:
        effective["max_cost_per_day"] = float(workspace.budget_max_daily_usd)
    if workspace.budget_max_gpus is not None:
        effective["max_gpus"] = workspace.budget_max_gpus

    return workspace, effective


async def count_org_workspaces(org_id: uuid.UUID, db: AsyncSession) -> int:
    """Count active (non-teardown/deleted) workspaces for an org."""
    result = await db.execute(
        select(func.count(Workspace.id)).where(
            Workspace.org_id == org_id,
            Workspace.status.notin_(["Teardown", "Deleted"]),
        )
    )
    return int(result.scalar() or 0)


async def count_workspace_nodes(workspace: Workspace, db: AsyncSession) -> int:
    """Count active nodes for a workspace."""
    if not workspace.cluster_id:
        return 0
    result = await db.execute(
        select(func.count(Node.id)).where(
            Node.cluster_id == workspace.cluster_id,
            Node.terminated_at.is_(None),
            Node.status.in_(["Running", "Provisioning", "Ready"]),
        )
    )
    return int(result.scalar() or 0)


async def count_workspace_gpus(workspace: Workspace, db: AsyncSession) -> int:
    """Count physical GPUs provisioned for the workspace's cluster.

    Model allocations use ``count_workspace_deployment_gpus`` separately: a GPU
    allocated to a model on an existing node must not be charged twice.
    """
    node_total = 0
    if workspace.cluster_id:
        result = await db.execute(
            select(func.coalesce(func.sum(Node.gpu_count), 0)).where(
                Node.cluster_id == workspace.cluster_id,
                Node.terminated_at.is_(None),
                Node.status.in_(["Running", "Provisioning", "Ready"]),
            )
        )
        node_total = int(result.scalar() or 0)

    return node_total


async def count_workspace_deployment_gpus(
    workspace_id: uuid.UUID, db: AsyncSession
) -> int:
    """Total GPUs committed by a workspace's live model deployments.

    ``replicas * gpu_per_replica`` per deployment, matching how a request's demand is
    computed, so the limit compares like with like.

    Keyed on ``workspace_id``, not ``cluster_id``: two workspaces can share one
    cluster, and charging each for the other's deployments would throttle both below
    their real entitlement.

    Deleted and failed deployments are excluded — they hold no capacity. A deployment
    still being created IS counted: it is a commitment already made, and ignoring it
    is what lets concurrent requests each see capacity the other is using.
    """
    result = await db.execute(
        select(
            func.coalesce(
                func.sum(
                    Deployment.desired_replicas
                    * func.coalesce(Deployment.gpu_per_replica, 0)
                ),
                0,
            )
        ).where(
            Deployment.workspace_id == workspace_id,
            Deployment.status.notin_(RELEASED_DEPLOYMENT_STATUSES),
        )
    )
    return int(result.scalar() or 0)


async def count_org_gpus(org_id: uuid.UUID, db: AsyncSession) -> int:
    """Count total active GPUs across all workspaces for an org."""
    result = await db.execute(
        select(func.coalesce(func.sum(Node.gpu_count), 0)).where(
            Node.org_id == org_id,
            Node.terminated_at.is_(None),
            Node.status.in_(["Running", "Provisioning", "Ready"]),
        )
    )
    return int(result.scalar() or 0)


async def count_org_nodes(org_id: uuid.UUID, db: AsyncSession) -> int:
    """Count total active nodes across all workspaces for an org."""
    result = await db.execute(
        select(func.count(Node.id)).where(
            Node.org_id == org_id,
            Node.terminated_at.is_(None),
            Node.status.in_(["Running", "Provisioning", "Ready"]),
        )
    )
    return int(result.scalar() or 0)


def mark_quota_decision(request: Request | None) -> None:
    """Record that a quota decision was evaluated for this request.

    The quota middleware only claims enforcement for requests carrying this mark, so
    the ``X-Quota-Enforcement`` header cannot appear on a path where nothing was
    checked (issue #5671, A15). ``None`` is accepted so the enforcement functions stay
    callable outside a request — from the reconciler and from tests — without the
    marking becoming a required argument everywhere.
    """
    if request is not None:
        setattr(request.state, QUOTA_DECISION_ATTR, True)


def raise_quota_exceeded(
    quota_type: str,
    current: int | float | Decimal,
    limit: int | float | Decimal,
    resource_type: str = "workspace",
    resource_id: str | None = None,
) -> None:
    """Raise HTTP 429 with a clear quota exceeded message.

    Args:
        quota_type: Type of quota (e.g., 'max_gpus', 'max_workspaces').
        current: Current usage value.
        limit: Quota limit value.
        resource_type: Type of resource being constrained.
        resource_id: Optional ID of the resource.
    """
    detail = (
        f"Quota exceeded: {quota_type} limit is {limit}, "
        f"current usage is {current}. "
        f"Contact your platform administrator to increase the quota."
    )
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={
            "X-Quota-Type": quota_type,
            "X-Quota-Current": str(current),
            "X-Quota-Limit": str(limit),
            "Retry-After": "60",
        },
    )


async def enforce_workspace_creation_quota(org_id: uuid.UUID, db: AsyncSession) -> None:
    """Check that the org can create another workspace.

    Raises:
        HTTPException 429 if workspace quota is exceeded.
    """
    _, quotas = await get_org_quotas(org_id, db)
    max_workspaces = quotas.get("max_workspaces")

    if max_workspaces is not None:
        current = await count_org_workspaces(org_id, db)
        if current >= max_workspaces:
            raise_quota_exceeded(
                "max_workspaces", current, max_workspaces, "org", str(org_id)
            )


async def enforce_node_provisioning_quota(
    workspace_id: uuid.UUID,
    org_id: uuid.UUID,
    gpu_count: int,
    cloud: str | None,
    db: AsyncSession,
) -> None:
    """Check quotas before provisioning a node.

    Validates:
    - Workspace max_nodes
    - Workspace max_gpus (current + requested)
    - Org-level max_nodes and max_gpus
    - Allowed clouds

    Raises:
        HTTPException 429 if any quota is exceeded.
    """
    workspace, ws_quotas = await get_workspace_quotas(workspace_id, org_id, db)

    # Check workspace max_nodes
    max_nodes = ws_quotas.get("max_nodes")
    if max_nodes is not None:
        current_nodes = await count_workspace_nodes(workspace, db)
        if current_nodes + 1 > max_nodes:
            raise_quota_exceeded(
                "max_nodes", current_nodes, max_nodes, "workspace", str(workspace_id)
            )

    # Check workspace max_gpus
    max_gpus = ws_quotas.get("max_gpus")
    if max_gpus is not None:
        current_gpus = await count_workspace_gpus(workspace, db)
        if current_gpus + gpu_count > max_gpus:
            raise_quota_exceeded(
                "max_gpus", current_gpus, max_gpus, "workspace", str(workspace_id)
            )

    # Check allowed clouds
    allowed_clouds = ws_quotas.get("allowed_clouds")
    if allowed_clouds and cloud:
        if cloud.lower() not in [c.lower() for c in allowed_clouds]:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Cloud provider '{cloud}' is not allowed. Allowed: {allowed_clouds}",
                headers={"X-Quota-Type": "allowed_clouds"},
            )

    # Check org-level quotas
    _, org_quotas = await get_org_quotas(org_id, db)

    org_max_nodes = org_quotas.get("max_nodes")
    if org_max_nodes is not None:
        org_nodes = await count_org_nodes(org_id, db)
        if org_nodes + 1 > org_max_nodes:
            raise_quota_exceeded(
                "max_nodes", org_nodes, org_max_nodes, "org", str(org_id)
            )

    org_max_gpus = org_quotas.get("max_gpus")
    if org_max_gpus is not None:
        org_gpus = await count_org_gpus(org_id, db)
        if org_gpus + gpu_count > org_max_gpus:
            raise_quota_exceeded("max_gpus", org_gpus, org_max_gpus, "org", str(org_id))


# `enforce_deployment_quota` was REMOVED here (issue #5671, A15).
#
# It performed exactly the check this change needed, and it had no call site anywhere in
# the repository — the contracts analysis had already flagged that ("declared with no
# call site anywhere — do not read their existence as enforcement",
# contracts/INTEGRATION-CONTRACT.md). Its body now lives inside
# `reserve_deployment_gpus`, which additionally holds the workspace lock and writes the
# reservation, so the two cannot be used interchangeably: this one checked and returned,
# leaving the caller to write the row in a second step, and that gap is precisely how two
# concurrent requests both pass against the same headroom.
#
# Deleted rather than kept "for previews", because a quota function with no caller is the
# defect this issue exists to fix, not a spare part. A preview endpoint, if one is ever
# wanted, should call the same routine the provisioning path does so the preview cannot
# drift from the decision.


def _assert_deployment_within_quota(
    workspace_id: uuid.UUID,
    ws_quotas: dict[str, Any],
    *,
    current_gpus: int,
    total_gpus_requested: int,
) -> None:
    """Raise 429 unless the request fits the workspace's GPU and daily-spend limits.

    The limit applies to the workspace's TOTAL committed capacity
    (``current_gpus + total_gpus_requested``), not to the size of a single request, so
    a series of individually-modest requests cannot walk past the budget.

    A workspace with no recorded limit falls back to its plan default (``PLAN_DEFAULTS``,
    via :func:`get_workspace_quotas`) rather than being treated as unlimited: an absent
    budget is an unconfigured workspace, and reading that as "no ceiling" is how an
    unconfigured tenant becomes the expensive one.
    """
    max_gpus = ws_quotas.get("max_gpus")
    if max_gpus is not None and current_gpus + total_gpus_requested > max_gpus:
        raise_quota_exceeded(
            "max_gpus",
            current_gpus,
            max_gpus,
            "workspace",
            str(workspace_id),
        )

    # Daily spend is checked separately, against cost actually recorded for today —
    # see `_assert_daily_spend_within_budget`. It is not derived from the GPU count
    # here: this service prices spend from each node's recorded `hourly_cost_usd`, and
    # there is no per-GPU list price to project a new deployment's cost from.
    # Inventing a rate would produce a limit that refuses legitimate requests whenever
    # the guess ran high, which is the over-strict failure mode this change must avoid.


async def _assert_daily_spend_within_budget(
    workspace: Workspace,
    ws_quotas: dict[str, Any],
    db: AsyncSession,
) -> None:
    """Raise 429 if the workspace has already spent its daily budget.

    Uses cost the platform has actually recorded for today (the cost reconciler's own
    calculation, from each node's recorded hourly rate) rather than a projection, so
    the figure in the refusal is one an operator can reconcile against the cost views.

    The limitation is worth being precise about: this refuses further provisioning once
    the budget is already spent, so it bounds how far a workspace can run over rather
    than predicting whether one specific new deployment would cross the line. Pricing a
    not-yet-created deployment would need a per-GPU rate this service does not have.
    """
    max_cost_per_day = ws_quotas.get("max_cost_per_day")
    if max_cost_per_day is None:
        return

    # Imported here, not at module scope: `app.services.cost_reconciler` imports this
    # module for quota figures, so a top-level import would be circular.
    from app.services.cost_reconciler import CostReconciler

    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    spent = await CostReconciler(db)._compute_daily_cost(workspace, day_start, now)

    if spent >= Decimal(str(max_cost_per_day)):
        raise_quota_exceeded(
            "max_cost_per_day",
            spent.quantize(Decimal("0.01")),
            max_cost_per_day,
            "workspace",
            str(workspace.id),
        )


async def reserve_deployment_gpus(
    workspace_id: uuid.UUID,
    org_id: uuid.UUID,
    total_gpus_requested: int,
    db: AsyncSession,
    *,
    deployment_kwargs: dict[str, Any],
    request: Request | None = None,
) -> Deployment:
    """Atomically check the workspace's GPU/spend budget and reserve the capacity.

    Issue #5671 (A15). Returns a persisted ``Deployment`` row in state ``Pending``,
    which counts toward the workspace's committed GPUs from the moment it is written.

    WHY THE ROW *IS* THE RESERVATION
    --------------------------------
    A separate "reserved capacity" counter would need its own compensation path, and a
    counter that is decremented by code that might not run is exactly the leak the
    acceptance criteria call out. Writing the deployment row up front means the
    reservation and the thing being reserved for are the same object: releasing it is a
    status change, and a crash leaves a visible row an operator can see rather than an
    invisible counter nobody can reconcile.

    WHY IT IS ATOMIC
    ----------------
    The workspace row is locked FOR UPDATE and the committed total is recounted *under
    that lock*, so two concurrent requests for the same workspace serialise: the second
    sees the first's reservation and is refused if it no longer fits. Without the lock
    both read the same pre-request total, both pass, and the workspace ends up over
    budget with neither request at fault.

    Uncertain provider writes retain capacity. The durable operation lifecycle
    releases it only after a confirmed terminal failure or confirmed deletion.

    Raises:
        HTTPException 429 if the GPU or daily-spend quota would be exceeded.
        HTTPException 404 if the workspace does not exist for this org.
    """
    # Record that a quota decision is being made for this request, so the middleware's
    # enforcement header reflects reality instead of asserting it. Set BEFORE the checks
    # below, because a refusal is a decision too — a 429 response should still be
    # marked as having been quota-evaluated.
    mark_quota_decision(request)

    # Lock the workspace row first. This is the serialisation point for every
    # reservation against this workspace; the quota figures are only meaningful once
    # no other request can be reading them concurrently.
    locked = await db.execute(
        select(Workspace)
        .where(Workspace.id == workspace_id, Workspace.org_id == org_id)
        .with_for_update()
    )
    workspace = locked.scalar_one_or_none()
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found"
        )

    _, ws_quotas = await get_workspace_quotas(workspace_id, org_id, db)

    # Recount under the lock — not before it — so a reservation committed by a
    # concurrent request since this request arrived is included.
    _assert_deployment_within_quota(
        workspace_id,
        ws_quotas,
        current_gpus=await count_workspace_deployment_gpus(workspace.id, db),
        total_gpus_requested=total_gpus_requested,
    )
    await _assert_daily_spend_within_budget(workspace, ws_quotas, db)

    deployment = Deployment(
        workspace_id=workspace_id,
        org_id=org_id,
        status=DEPLOYMENT_STATUS_RESERVED,
        **deployment_kwargs,
    )
    db.add(deployment)
    # Commit while still holding the lock's transaction, so the reservation is visible
    # to the next request before this one proceeds to the cluster.
    await db.commit()
    await db.refresh(deployment)
    return deployment
