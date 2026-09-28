"""Periodic budget enforcement from recorded node-rate estimates.

These estimates are not reconciled provider bills. Missing node rates cannot
establish a complete total; public cost/budget responses expose that distinction.
The existing enforcement calculation uses the known subtotal to detect threshold
breaches, while paid operation admission separately reserves approved budgets.

Runs on a configurable interval (default 60s) to:
1. Aggregate daily costs per workspace from node hourly rates
2. Compare against workspace budget_max_daily_usd and budget_max_gpus
3. Create BudgetAlert records and emit events on threshold breaches
4. Mark workspaces as budget_exceeded when hard limits are hit

Budget thresholds:
- 80% of daily budget  -> warning alert
- 100% of daily budget -> critical alert + workspace status set to budget_exceeded
- GPU limit exceeded   -> critical alert + workspace status set to budget_exceeded
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.budget_alert import BudgetAlert
from app.models.event import Event
from app.models.node import Node
from app.models.reconcile_lock import ReconcileLock
from app.models.workspace import Workspace
from app.services.node_cost_estimates import estimate, window, workspace_nodes

logger = logging.getLogger(__name__)

# Budget enforcement thresholds
BUDGET_WARNING_PCT = Decimal("80")
BUDGET_CRITICAL_PCT = Decimal("100")

# Lock parameters
LOCK_RESOURCE_TYPE = "cost_reconciler"
LOCK_RESOURCE_ID = "global"
LOCK_TTL_SECONDS = 120


class CostReconciler:
    """Reconciles workspace costs against budget limits.

    Designed to be called periodically (e.g., every 60 seconds) by a background
    task or cron job. Uses a reconcile lock to prevent concurrent execution.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def reconcile(self) -> dict[str, Any]:
        """Run a full reconciliation cycle.

        Returns:
            Summary dict with workspaces checked, alerts created, and actions taken.
        """
        if not await self._acquire_lock():
            logger.debug("CostReconciler: lock held by another instance, skipping")
            return {"status": "skipped", "reason": "lock_held"}

        try:
            result = await self._run_reconciliation()
            return result
        finally:
            await self._release_lock()

    async def _acquire_lock(self) -> bool:
        """Acquire the reconcile lock using the existing ReconcileLock model."""
        now = datetime.now(timezone.utc)

        # Clean up expired locks
        await self.db.execute(
            delete(ReconcileLock).where(
                ReconcileLock.resource_type == LOCK_RESOURCE_TYPE,
                ReconcileLock.resource_id == LOCK_RESOURCE_ID,
                ReconcileLock.expires_at < now,
            )
        )

        # Check if lock is held
        existing = await self.db.execute(
            select(ReconcileLock).where(
                ReconcileLock.resource_type == LOCK_RESOURCE_TYPE,
                ReconcileLock.resource_id == LOCK_RESOURCE_ID,
            )
        )
        if existing.scalar_one_or_none() is not None:
            return False

        # Create lock
        lock = ReconcileLock(
            resource_type=LOCK_RESOURCE_TYPE,
            resource_id=LOCK_RESOURCE_ID,
            locked_by="cost-reconciler",
            expires_at=now + timedelta(seconds=LOCK_TTL_SECONDS),
        )
        self.db.add(lock)
        await self.db.flush()
        return True

    async def _release_lock(self) -> None:
        """Release the reconcile lock."""
        await self.db.execute(
            delete(ReconcileLock).where(
                ReconcileLock.resource_type == LOCK_RESOURCE_TYPE,
                ReconcileLock.resource_id == LOCK_RESOURCE_ID,
            )
        )
        await self.db.flush()

    async def _run_reconciliation(self) -> dict[str, Any]:
        """Core reconciliation logic: check all active workspaces against budgets."""
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Get all active workspaces that have budget limits configured
        ws_result = await self.db.execute(
            select(Workspace).where(
                Workspace.status.in_(["active", "running", "budget_warning"]),
            )
        )
        workspaces = ws_result.scalars().all()

        summary: dict[str, Any] = {
            "status": "completed",
            "workspaces_checked": 0,
            "warnings_created": 0,
            "violations_created": 0,
            "workspaces_suspended": 0,
            "timestamp": now.isoformat(),
        }

        for ws in workspaces:
            summary["workspaces_checked"] += 1
            ws_actions = await self._check_workspace(ws, day_start, now)
            summary["warnings_created"] += ws_actions.get("warnings", 0)
            summary["violations_created"] += ws_actions.get("violations", 0)
            summary["workspaces_suspended"] += ws_actions.get("suspended", 0)

        await self.db.commit()
        logger.info(
            "CostReconciler completed: checked=%d warnings=%d violations=%d suspended=%d",
            summary["workspaces_checked"],
            summary["warnings_created"],
            summary["violations_created"],
            summary["workspaces_suspended"],
        )
        return summary

    async def _check_workspace(
        self,
        workspace: Workspace,
        day_start: datetime,
        now: datetime,
    ) -> dict[str, int]:
        """Check a single workspace against its budget limits.

        Returns:
            Dict with counts of warnings, violations, and suspensions.
        """
        actions = {"warnings": 0, "violations": 0, "suspended": 0}

        # Check daily cost budget
        if workspace.budget_max_daily_usd is not None:
            cost_actions = await self._check_daily_cost(workspace, day_start, now)
            for k, v in cost_actions.items():
                actions[k] = actions.get(k, 0) + v

        # Check GPU limit
        if workspace.budget_max_gpus is not None:
            gpu_actions = await self._check_gpu_limit(workspace)
            for k, v in gpu_actions.items():
                actions[k] = actions.get(k, 0) + v

        return actions

    async def _check_daily_cost(
        self,
        workspace: Workspace,
        day_start: datetime,
        now: datetime,
    ) -> dict[str, int]:
        """Check workspace daily cost against budget_max_daily_usd."""
        actions = {"warnings": 0, "violations": 0, "suspended": 0}
        daily_cost = await self._compute_daily_cost(workspace, day_start, now)
        budget_limit = workspace.budget_max_daily_usd

        if budget_limit is None or budget_limit <= 0:
            return actions

        pct_used = (daily_cost / budget_limit) * Decimal("100")

        # Critical: budget exceeded
        if pct_used >= BUDGET_CRITICAL_PCT:
            if not await self._has_active_alert(workspace.id, "budget_exceeded"):
                await self._create_alert(
                    workspace=workspace,
                    alert_type="budget_exceeded",
                    severity="critical",
                    threshold_pct=pct_used,
                    current_value=daily_cost,
                    limit_value=budget_limit,
                    message=f"Daily cost ${daily_cost:.2f} exceeds budget ${budget_limit:.2f} ({pct_used:.1f}%)",
                )
                actions["violations"] += 1

            # Suspend workspace
            if workspace.status != "budget_exceeded":
                await self._suspend_workspace(workspace, daily_cost, budget_limit)
                actions["suspended"] += 1

        # Warning: approaching budget
        elif pct_used >= BUDGET_WARNING_PCT:
            if not await self._has_active_alert(workspace.id, "budget_warning"):
                await self._create_alert(
                    workspace=workspace,
                    alert_type="budget_warning",
                    severity="warning",
                    threshold_pct=pct_used,
                    current_value=daily_cost,
                    limit_value=budget_limit,
                    message=f"Daily cost ${daily_cost:.2f} is at {pct_used:.1f}% of budget ${budget_limit:.2f}",
                )
                actions["warnings"] += 1

                # Update workspace status to indicate warning
                if workspace.status not in ("budget_exceeded", "budget_warning"):
                    workspace.status = "budget_warning"
                    await self.db.flush()

        return actions

    async def _check_gpu_limit(self, workspace: Workspace) -> dict[str, int]:
        """Check workspace active GPU count against budget_max_gpus."""
        actions = {"warnings": 0, "violations": 0, "suspended": 0}

        if workspace.budget_max_gpus is None:
            return actions

        active_gpus = await self._count_active_gpus(workspace)

        if active_gpus > workspace.budget_max_gpus:
            if not await self._has_active_alert(workspace.id, "gpu_limit_exceeded"):
                await self._create_alert(
                    workspace=workspace,
                    alert_type="gpu_limit_exceeded",
                    severity="critical",
                    threshold_pct=Decimal(
                        str((active_gpus / workspace.budget_max_gpus) * 100)
                    ),
                    current_value=Decimal(str(active_gpus)),
                    limit_value=Decimal(str(workspace.budget_max_gpus)),
                    message=f"Active GPUs ({active_gpus}) exceeds limit ({workspace.budget_max_gpus})",
                )
                actions["violations"] += 1

            if workspace.status != "budget_exceeded":
                await self._suspend_workspace(
                    workspace,
                    Decimal(str(active_gpus)),
                    Decimal(str(workspace.budget_max_gpus)),
                )
                actions["suspended"] += 1

        return actions

    async def _compute_daily_cost(
        self,
        workspace: Workspace,
        day_start: datetime,
        now: datetime,
    ) -> Decimal:
        """Compute the known node-rate subtotal for threshold enforcement.

        Sums hourly_cost_usd * hours_running for all nodes in the workspace's cluster
        that were active during the current day. Missing rates contribute nothing
        to this lower bound; it must not be presented as a complete or billed total.
        """
        if not workspace.cluster_id:
            return Decimal("0")

        node_result = await self.db.execute(
            select(Node).where(
                Node.org_id == workspace.org_id,
                Node.cluster_id == workspace.cluster_id,
                # Include nodes that were active at any point today
                Node.created_at <= now,
                # Exclude nodes terminated before today
                (Node.terminated_at.is_(None)) | (Node.terminated_at >= day_start),
            )
        )
        nodes = node_result.scalars().all()

        total_cost = Decimal("0")
        for node in nodes:
            hourly_rate = node.hourly_cost_usd or Decimal("0")

            # Node timestamps are stored as UTC. SQLite returns them without
            # tzinfo, unlike PostgreSQL's timestamptz; restore that UTC meaning.
            created_at = node.created_at
            if created_at is not None and created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            terminated_at = node.terminated_at
            if terminated_at is not None and terminated_at.tzinfo is None:
                terminated_at = terminated_at.replace(tzinfo=timezone.utc)
            start = max(created_at, day_start) if created_at else day_start
            end = terminated_at or now
            if end > now:
                end = now
            if end < day_start:
                end = day_start

            if end <= start:
                continue

            delta = end - start
            hours = Decimal(str(delta.total_seconds())) / Decimal("3600")
            total_cost += hourly_rate * hours

        return total_cost

    async def _count_active_gpus(self, workspace: Workspace) -> int:
        """Count total active (non-terminated) GPUs for a workspace."""
        if not workspace.cluster_id:
            return 0

        result = await self.db.execute(
            select(func.coalesce(func.sum(Node.gpu_count), 0)).where(
                Node.org_id == workspace.org_id,
                Node.cluster_id == workspace.cluster_id,
                Node.terminated_at.is_(None),
                Node.status.in_(["Running", "Provisioning", "Ready"]),
            )
        )
        return int(result.scalar() or 0)

    async def _has_active_alert(self, workspace_id: uuid.UUID, alert_type: str) -> bool:
        """Check if there's an unresolved alert of this type for the workspace today."""
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        result = await self.db.execute(
            select(BudgetAlert.id)
            .where(
                BudgetAlert.workspace_id == workspace_id,
                BudgetAlert.alert_type == alert_type,
                BudgetAlert.resolved_at.is_(None),
                BudgetAlert.created_at >= today_start,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def _create_alert(
        self,
        workspace: Workspace,
        alert_type: str,
        severity: str,
        threshold_pct: Decimal,
        current_value: Decimal,
        limit_value: Decimal,
        message: str,
    ) -> BudgetAlert:
        """Create a BudgetAlert and corresponding Event."""
        alert = BudgetAlert(
            org_id=workspace.org_id,
            workspace_id=workspace.id,
            alert_type=alert_type,
            severity=severity,
            threshold_pct=threshold_pct,
            current_value=current_value,
            limit_value=limit_value,
            message=message,
        )
        self.db.add(alert)

        # Also create an audit event
        event = Event(
            org_id=workspace.org_id,
            resource_type="workspace",
            resource_id=workspace.id,
            event_type=f"budget.{alert_type}",
            message=message,
            details_json=json.dumps(
                {
                    "alert_type": alert_type,
                    "severity": severity,
                    "threshold_pct": str(threshold_pct),
                    "current_value": str(current_value),
                    "limit_value": str(limit_value),
                }
            ),
        )
        self.db.add(event)
        await self.db.flush()

        logger.warning(
            "Budget alert: workspace=%s type=%s severity=%s — %s",
            workspace.id,
            alert_type,
            severity,
            message,
        )
        return alert

    async def _suspend_workspace(
        self,
        workspace: Workspace,
        current_value: Decimal,
        limit_value: Decimal,
    ) -> None:
        """Mark a workspace as budget_exceeded."""
        old_status = workspace.status
        workspace.status = "budget_exceeded"
        await self.db.flush()

        event = Event(
            org_id=workspace.org_id,
            resource_type="workspace",
            resource_id=workspace.id,
            event_type="budget.workspace_suspended",
            message=f"Workspace suspended: cost/usage {current_value} exceeds limit {limit_value}",
            details_json=json.dumps(
                {
                    "previous_status": old_status,
                    "new_status": "budget_exceeded",
                    "current_value": str(current_value),
                    "limit_value": str(limit_value),
                }
            ),
        )
        self.db.add(event)
        await self.db.flush()

        logger.warning(
            "Workspace %s suspended due to budget exceeded (was: %s)",
            workspace.id,
            old_status,
        )


async def get_org_cost_summary(
    org_id: uuid.UUID,
    db: AsyncSession,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate cost data across all workspaces for an organization.

    Returns:
        Cost summary with per-workspace breakdown and org-level totals.
    """
    now = datetime.now(timezone.utc)

    # Get all workspaces for the org
    ws_result = await db.execute(select(Workspace).where(Workspace.org_id == org_id))
    workspaces = ws_result.scalars().all()

    start, end = window(start_date, end_date, now)
    workspace_costs = []
    # A shared cluster may appear in several workspaces. Organization estimates
    # count each recorded node once; workspace rows explicitly describe a cluster.
    org_nodes = {}
    incomplete_workspace = False
    for ws in workspaces:
        nodes = await workspace_nodes(db, ws, start, end)
        costs = estimate(nodes, start, end).values
        incomplete_workspace = (
            incomplete_workspace or costs["estimate_status"] != "available"
        )
        org_nodes.update((str(node.id), node) for node in nodes)
        workspace_costs.append(
            {
                "workspace_id": str(ws.id),
                "workspace_name": ws.name,
                **{
                    key: value
                    for key, value in costs.items()
                    if key not in {"nodes", "breakdown_by_gpu", "breakdown_by_cloud"}
                },
                "cost_scope": "workspace_cluster",
                "status": ws.status,
                "budget_max_daily_usd": str(ws.budget_max_daily_usd)
                if ws.budget_max_daily_usd is not None
                else None,
                "budget_max_gpus": ws.budget_max_gpus,
            }
        )
    costs = estimate(list(org_nodes.values()), start, end).values
    if incomplete_workspace:
        costs["total_cost_usd"] = None
        if costs["estimate_status"] == "available":
            costs["estimate_status"] = "partial"
    costs.pop("nodes")

    # Get active budget alerts for the org
    alert_result = await db.execute(
        select(BudgetAlert)
        .where(
            BudgetAlert.org_id == org_id,
            BudgetAlert.resolved_at.is_(None),
        )
        .order_by(BudgetAlert.created_at.desc())
        .limit(50)
    )
    alerts = alert_result.scalars().all()

    alert_list = [
        {
            "id": str(a.id),
            "workspace_id": str(a.workspace_id),
            "alert_type": a.alert_type,
            "severity": a.severity,
            "message": a.message,
            "current_value": str(a.current_value)
            if a.current_value is not None
            else None,
            "limit_value": str(a.limit_value) if a.limit_value is not None else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in alerts
    ]

    return {
        "org_id": str(org_id),
        **costs,
        "currency": "USD",
        "cost_scope": "organization_nodes",
        "checked_at": now.isoformat(),
        "workspace_count": len(workspaces),
        "workspaces": workspace_costs,
        "active_alerts": alert_list,
        "period": {
            "start": start.isoformat() if start else None,
            "end": end.isoformat(),
        },
    }


async def get_workspace_budget_status(
    workspace_id: uuid.UUID,
    org_id: uuid.UUID,
    db: AsyncSession,
) -> dict[str, Any]:
    """Get budget status for a specific workspace including alerts and current usage.

    Returns:
        Budget status dict with current costs, limits, and active alerts.
    """
    ws_result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.org_id == org_id,
        )
    )
    workspace = ws_result.scalar_one_or_none()
    if workspace is None:
        return {"error": "Workspace not found", "status_code": 404}

    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Compute today's cost
    reconciler = CostReconciler(db)
    daily_estimate = estimate(
        await workspace_nodes(db, workspace, day_start, now), day_start, now
    )
    costs = daily_estimate.values
    daily_cost = daily_estimate.total_usd
    active_gpus = await reconciler._count_active_gpus(workspace)

    # Get active alerts
    alert_result = await db.execute(
        select(BudgetAlert)
        .where(
            BudgetAlert.workspace_id == workspace_id,
            BudgetAlert.resolved_at.is_(None),
        )
        .order_by(BudgetAlert.created_at.desc())
        .limit(20)
    )
    alerts = alert_result.scalars().all()

    budget_limit = workspace.budget_max_daily_usd
    budget_pct = None
    if budget_limit is not None and budget_limit > 0 and daily_cost is not None:
        budget_pct = str(
            ((daily_cost / budget_limit) * Decimal("100")).quantize(Decimal("0.1"))
        )

    return {
        "workspace_id": str(workspace_id),
        "workspace_name": workspace.name,
        "status": workspace.status,
        "budget": {
            "max_daily_usd": str(budget_limit) if budget_limit is not None else None,
            "max_gpus": workspace.budget_max_gpus,
            "current_daily_cost_usd": costs["total_cost_usd"],
            "known_subtotal_usd": costs["known_subtotal_usd"],
            "estimate_status": costs["estimate_status"],
            "cost_basis": costs["cost_basis"],
            "cost_scope": "workspace_cluster",
            "observed_cost_usd": None,
            "cost_reconciliation": "unavailable",
            "unestimated_node_count": costs["unestimated_node_count"],
            "checked_at": now.isoformat(),
            "current_active_gpus": active_gpus,
            "daily_budget_used_pct": budget_pct,
        },
        "active_alerts": [
            {
                "id": str(a.id),
                "alert_type": a.alert_type,
                "severity": a.severity,
                "message": a.message,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in alerts
        ],
    }
