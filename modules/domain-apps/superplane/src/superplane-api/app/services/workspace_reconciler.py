"""WorkspaceReconciler — failed bootstrap retry + drift detection (US-H3).

This control plane reconciler handles two key scenarios:
1. **Failed bootstrap retry**: When a workspace bootstrap fails (status='Failed'),
   the reconciler automatically retries by re-triggering the bootstrap workflow
   with exponential backoff (60s base, max 5 retries).
2. **Drift detection**: Compares the associated cluster's desired_state_json vs
   actual_state_json to detect configuration drift and trigger re-bootstrap.

Reconciliation loop:
  1. Query all workspaces that need attention:
     - status == "Failed" with retry backoff elapsed and retries < max
     - status == "active" with drift detected on their cluster
  2. For each workspace:
     a. Acquire a reconcile lock to prevent concurrent reconciliation
     b. Re-trigger bootstrap workflow (for failures) or mark drift (for drift)
     c. Update workspace reconciler tracking fields
     d. Emit audit event
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.cluster import Cluster
from app.models.event import Event
from app.models.reconcile_lock import ReconcileLock
from app.models.workspace import (
    STATUS_ACTIVE,
    STATUS_DRIFT_DETECTED,
    STATUS_FAILED,
    STATUS_MAX_RETRIES_EXCEEDED,
    STATUS_RECONCILING,
    Workspace,
)

logger = logging.getLogger(__name__)

# --- Constants ---

# How often the reconciler runs its loop.
RECONCILE_INTERVAL_SECONDS = 60

# Base backoff for retrying failed bootstraps (exponential: base * 2^retry).
RETRY_BACKOFF_BASE_SECONDS = 60

# Maximum number of bootstrap retries before giving up.
MAX_BOOTSTRAP_RETRIES = 5

# Maximum backoff cap (16 minutes).
MAX_BACKOFF_SECONDS = 960

# Lock TTL — prevents stale locks from blocking forever.
LOCK_TTL_SECONDS = 120

# Keys to compare for drift detection between desired and actual cluster state.
DRIFT_DETECTION_KEYS = [
    "instance_type",
    "node_count",
    "gpu_type",
    "gpu_count",
    "kubernetes_version",
    "ami_id",
    "vpc_id",
    "subnet_ids",
    "security_group_ids",
    "iam_role_arn",
    "agent_input_queue_url",
    "agent_response_queue_url",
    "assume_role_arn",
]


def compute_backoff(retry_count: int, base: int = RETRY_BACKOFF_BASE_SECONDS) -> int:
    """Compute exponential backoff delay in seconds.

    Formula: min(base * 2^retry_count, MAX_BACKOFF_SECONDS)
    """
    delay = base * (2**retry_count)
    return min(delay, MAX_BACKOFF_SECONDS)


def detect_drift(desired: dict | None, actual: dict | None) -> dict:
    """Compare desired vs actual cluster state and return drifted keys.

    Only compares keys present in DRIFT_DETECTION_KEYS that exist in the
    desired state. Returns a dict of {key: {"desired": X, "actual": Y}}
    for each drifted key.
    """
    if not desired:
        return {}
    if not actual:
        # If there's no actual state but there is a desired state, that's drift
        # but only for keys that exist in desired
        return {
            k: {"desired": desired[k], "actual": None}
            for k in DRIFT_DETECTION_KEYS
            if k in desired
        }

    drifts = {}
    for key in DRIFT_DETECTION_KEYS:
        if key in desired:
            desired_val = desired[key]
            actual_val = actual.get(key)
            if desired_val != actual_val:
                drifts[key] = {"desired": desired_val, "actual": actual_val}

    return drifts


def _parse_json_state(state) -> dict:
    """Safely parse a state value that may be dict, str, or None."""
    if state is None:
        return {}
    if isinstance(state, dict):
        return state
    try:
        return json.loads(state)
    except (json.JSONDecodeError, TypeError):
        return {}


class WorkspaceReconciler:
    """Control plane reconciler for workspace bootstrap retry and drift detection.

    Watches:
      - Workspace rows with status == "Failed" (retry eligible)
      - Workspace rows with status == "active" (drift check)

    Actions:
      - Re-triggers bootstrap workflow for failed workspaces
      - Detects and flags configuration drift
      - Emits events for retries, drift detection, and max retry exceeded
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        reconcile_interval: int = RECONCILE_INTERVAL_SECONDS,
        max_retries: int = MAX_BOOTSTRAP_RETRIES,
        lock_ttl: int = LOCK_TTL_SECONDS,
        backoff_base: int = RETRY_BACKOFF_BASE_SECONDS,
    ):
        self._session_factory = session_factory
        self._reconcile_interval = reconcile_interval
        self._max_retries = max_retries
        self._lock_ttl = lock_ttl
        self._backoff_base = backoff_base
        self._running = False
        self._task: asyncio.Task | None = None

    # --- Lifecycle ---

    async def start(self) -> None:
        """Start the reconciliation loop as a background task."""
        if self._running:
            logger.warning("WorkspaceReconciler already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "WorkspaceReconciler started (interval=%ds)", self._reconcile_interval
        )

    async def stop(self) -> None:
        """Stop the reconciliation loop gracefully."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("WorkspaceReconciler stopped")

    async def _run_loop(self) -> None:
        """Main reconciliation loop — runs until stopped."""
        while self._running:
            try:
                stats = await self.reconcile()
                logger.info(
                    "WorkspaceReconciler tick: retried=%d, drift=%d, failed=%d, skipped=%d",
                    stats["retried"],
                    stats["drift_detected"],
                    stats["failed"],
                    stats["skipped"],
                )
            except Exception:
                logger.exception("WorkspaceReconciler tick failed")
            await asyncio.sleep(self._reconcile_interval)

    # --- Core Reconciliation ---

    async def reconcile(self) -> dict:
        """Run a single reconciliation pass.

        Returns stats dict: {"retried": N, "drift_detected": N, "failed": N,
                             "skipped": N, "max_retries_exceeded": N}
        """
        stats = {
            "retried": 0,
            "drift_detected": 0,
            "failed": 0,
            "skipped": 0,
            "max_retries_exceeded": 0,
        }

        async with self._session_factory() as session:
            # Phase 1: Retry failed bootstraps
            failed_workspaces = await self._find_failed_workspaces(session)
            for workspace in failed_workspaces:
                try:
                    result = await self._retry_bootstrap(session, workspace)
                    if result == "retried":
                        stats["retried"] += 1
                    elif result == "max_retries_exceeded":
                        stats["max_retries_exceeded"] += 1
                    elif result == "skipped":
                        stats["skipped"] += 1
                except Exception as exc:
                    logger.exception(
                        "Failed to retry bootstrap for workspace %s", workspace.id
                    )
                    await self._update_reconcile_error(session, workspace, str(exc))
                    stats["failed"] += 1

            # Phase 2: Drift detection on active workspaces
            active_workspaces = await self._find_active_workspaces_with_clusters(
                session
            )
            for workspace, cluster in active_workspaces:
                try:
                    drifted = await self._check_drift(session, workspace, cluster)
                    if drifted:
                        stats["drift_detected"] += 1
                except Exception as exc:
                    logger.exception(
                        "Failed drift check for workspace %s", workspace.id
                    )
                    await self._update_reconcile_error(session, workspace, str(exc))
                    stats["failed"] += 1

        return stats

    # --- Failed Bootstrap Retry ---

    async def _find_failed_workspaces(self, session: AsyncSession) -> list:
        """Find workspaces with Failed status eligible for retry."""
        stmt = select(Workspace).where(
            Workspace.status == STATUS_FAILED,
            Workspace.bootstrap_retry_count < self._max_retries,
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def _retry_bootstrap(
        self, session: AsyncSession, workspace: Workspace
    ) -> str:
        """Attempt to retry a failed workspace bootstrap.

        Returns: "retried", "max_retries_exceeded", or "skipped"
        """
        now = datetime.now(timezone.utc)

        # Check if max retries exceeded
        if workspace.bootstrap_retry_count >= self._max_retries:
            await self._mark_max_retries_exceeded(session, workspace)
            return "max_retries_exceeded"

        # Check backoff — is it too soon to retry?
        if workspace.last_bootstrap_at:
            backoff = compute_backoff(
                workspace.bootstrap_retry_count, self._backoff_base
            )
            next_retry_at = workspace.last_bootstrap_at + timedelta(seconds=backoff)
            if now < next_retry_at:
                logger.debug(
                    "Workspace %s: backoff not elapsed (retry in %ds)",
                    workspace.id,
                    (next_retry_at - now).total_seconds(),
                )
                return "skipped"

        # Acquire lock
        lock_key = f"workspace-reconcile:{workspace.id}"
        locked = await self._acquire_lock(session, lock_key)
        if not locked:
            logger.debug("Workspace %s: already locked, skipping", workspace.id)
            return "skipped"

        try:
            # Trigger bootstrap workflow
            from app.services.github import trigger_bootstrap

            triggered = await trigger_bootstrap(
                workspace_id=str(workspace.id),
                workspace_name=workspace.name,
                org_id=str(workspace.org_id),
                isolation_mode=workspace.isolation_mode,
            )

            new_retry_count = workspace.bootstrap_retry_count + 1

            if triggered:
                await session.execute(
                    update(Workspace)
                    .where(Workspace.id == workspace.id)
                    .values(
                        status=STATUS_RECONCILING,
                        bootstrap_retry_count=new_retry_count,
                        last_bootstrap_at=now,
                        reconcile_error=None,
                    )
                )
                await session.commit()

                await self._emit_event(
                    session,
                    org_id=workspace.org_id,
                    resource_id=workspace.id,
                    event_type="WorkspaceBootstrapRetry",
                    message=(
                        f"Retrying bootstrap for workspace '{workspace.name}' "
                        f"(attempt {new_retry_count}/{self._max_retries})"
                    ),
                    details={
                        "workspace_id": str(workspace.id),
                        "retry_count": new_retry_count,
                        "max_retries": self._max_retries,
                    },
                )

                logger.info(
                    "Workspace %s: bootstrap retry triggered (attempt %d/%d)",
                    workspace.id,
                    new_retry_count,
                    self._max_retries,
                )
                return "retried"
            else:
                error_msg = "Failed to trigger bootstrap workflow via GitHub Actions"
                await session.execute(
                    update(Workspace)
                    .where(Workspace.id == workspace.id)
                    .values(
                        bootstrap_retry_count=new_retry_count,
                        last_bootstrap_at=now,
                        reconcile_error=error_msg,
                    )
                )
                await session.commit()
                logger.warning("Workspace %s: bootstrap trigger failed", workspace.id)
                return "skipped"

        finally:
            await self._release_lock(session, lock_key)

    async def _mark_max_retries_exceeded(
        self, session: AsyncSession, workspace: Workspace
    ) -> None:
        """Mark a workspace as having exceeded max bootstrap retries."""
        await session.execute(
            update(Workspace)
            .where(Workspace.id == workspace.id)
            .values(
                status=STATUS_MAX_RETRIES_EXCEEDED,
                reconcile_error=(
                    f"Max bootstrap retries ({self._max_retries}) exceeded"
                ),
            )
        )
        await session.commit()

        await self._emit_event(
            session,
            org_id=workspace.org_id,
            resource_id=workspace.id,
            event_type="WorkspaceMaxRetriesExceeded",
            message=(
                f"Workspace '{workspace.name}' exceeded max bootstrap retries "
                f"({self._max_retries}). Manual intervention required."
            ),
            details={
                "workspace_id": str(workspace.id),
                "retry_count": workspace.bootstrap_retry_count,
                "max_retries": self._max_retries,
            },
        )

        logger.warning(
            "Workspace %s: max bootstrap retries (%d) exceeded",
            workspace.id,
            self._max_retries,
        )

    # --- Drift Detection ---

    async def _find_active_workspaces_with_clusters(
        self, session: AsyncSession
    ) -> list[tuple[Workspace, Cluster]]:
        """Find active workspaces that have an associated cluster for drift checking."""
        stmt = (
            select(Workspace, Cluster)
            .join(Cluster, Workspace.cluster_id == Cluster.id)
            .where(
                Workspace.status.in_([STATUS_ACTIVE, "running"]),
                Workspace.cluster_id.is_not(None),
                Cluster.status.in_(["Active", "Running", "Healthy"]),
            )
        )
        result = await session.execute(stmt)
        return [tuple(row) for row in result.all()]  # type: ignore[return-value]

    async def _check_drift(
        self,
        session: AsyncSession,
        workspace: Workspace,
        cluster: Cluster,
    ) -> bool:
        """Check for configuration drift on a workspace's cluster.

        Returns True if drift was detected.
        """
        now = datetime.now(timezone.utc)

        desired = _parse_json_state(cluster.desired_state_json)
        actual = _parse_json_state(cluster.actual_state_json)

        drifts = detect_drift(desired, actual)

        # Update last drift check timestamp
        await session.execute(
            update(Workspace)
            .where(Workspace.id == workspace.id)
            .values(last_drift_check_at=now)
        )

        if drifts:
            logger.warning(
                "Workspace %s: drift detected on cluster %s — keys: %s",
                workspace.id,
                cluster.id,
                list(drifts.keys()),
            )

            await session.execute(
                update(Workspace)
                .where(Workspace.id == workspace.id)
                .values(
                    status=STATUS_DRIFT_DETECTED,
                    reconcile_error=f"Drift detected: {json.dumps(drifts)}",
                )
            )
            await session.commit()

            await self._emit_event(
                session,
                org_id=workspace.org_id,
                resource_id=workspace.id,
                event_type="WorkspaceDriftDetected",
                message=(
                    f"Configuration drift detected on workspace '{workspace.name}': "
                    f"{list(drifts.keys())}"
                ),
                details={
                    "workspace_id": str(workspace.id),
                    "cluster_id": str(cluster.id),
                    "drifted_keys": drifts,
                },
            )

            return True

        await session.commit()
        return False

    # --- Manual Trigger ---

    async def reconcile_workspace(self, workspace_id: uuid.UUID) -> dict:
        """Manually trigger reconciliation for a specific workspace.

        Returns a result dict describing what action was taken.
        """
        async with self._session_factory() as session:
            # Find the workspace
            result = await session.execute(
                select(Workspace).where(Workspace.id == workspace_id)
            )
            workspace = result.scalar_one_or_none()

            if workspace is None:
                return {"status": "error", "message": "Workspace not found"}

            # If workspace is in Failed state, attempt retry
            if workspace.status in (STATUS_FAILED, STATUS_MAX_RETRIES_EXCEEDED):
                # Reset retry count for manual trigger
                await session.execute(
                    update(Workspace)
                    .where(Workspace.id == workspace.id)
                    .values(
                        bootstrap_retry_count=0,
                        status=STATUS_FAILED,
                        reconcile_error=None,
                    )
                )
                await session.commit()

                # Re-fetch after reset
                result = await session.execute(
                    select(Workspace).where(Workspace.id == workspace_id)
                )
                workspace = result.scalar_one_or_none()
                if workspace is None:
                    return {
                        "status": "error",
                        "action": "bootstrap_retry",
                        "message": "Workspace not found after reset",
                        "workspace_id": str(workspace_id),
                    }

                retry_result = await self._retry_bootstrap(session, workspace)
                return {
                    "status": "ok",
                    "action": "bootstrap_retry",
                    "result": retry_result,
                    "workspace_id": str(workspace_id),
                }

            # If workspace has a cluster, check drift
            if workspace.cluster_id and workspace.status in (
                STATUS_ACTIVE,
                "running",
                STATUS_DRIFT_DETECTED,
            ):
                cluster_result = await session.execute(
                    select(Cluster).where(Cluster.id == workspace.cluster_id)
                )
                cluster = cluster_result.scalar_one_or_none()

                if cluster:
                    drifted = await self._check_drift(session, workspace, cluster)
                    return {
                        "status": "ok",
                        "action": "drift_check",
                        "drift_detected": drifted,
                        "workspace_id": str(workspace_id),
                    }

            return {
                "status": "ok",
                "action": "no_action",
                "message": f"Workspace status '{workspace.status}' does not require reconciliation",
                "workspace_id": str(workspace_id),
            }

    # --- Helpers ---

    async def _update_reconcile_error(
        self, session: AsyncSession, workspace: Workspace, error: str
    ) -> None:
        """Update the reconcile error on a workspace."""
        await session.execute(
            update(Workspace)
            .where(Workspace.id == workspace.id)
            .values(reconcile_error=error)
        )
        await session.commit()

    async def _emit_event(
        self,
        session: AsyncSession,
        org_id: uuid.UUID,
        resource_id: uuid.UUID,
        event_type: str,
        message: str,
        details: dict | None = None,
    ) -> None:
        """Emit a platform event."""
        event = Event(
            org_id=org_id,
            resource_type="Workspace",
            resource_id=resource_id,
            event_type=event_type,
            message=message,
            details_json=json.dumps(details) if details else None,
        )
        session.add(event)
        await session.commit()

    # --- Locking ---

    async def _acquire_lock(self, session: AsyncSession, lock_key: str) -> bool:
        """Attempt to acquire a reconcile lock. Returns True if acquired."""
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=self._lock_ttl)

        # Clean up expired locks first
        await session.execute(
            delete(ReconcileLock).where(
                ReconcileLock.resource_type == "workspace_reconciler",
                ReconcileLock.resource_id == lock_key,
                ReconcileLock.expires_at < now,
            )
        )

        # Check if lock exists and is active
        stmt = select(ReconcileLock).where(
            ReconcileLock.resource_type == "workspace_reconciler",
            ReconcileLock.resource_id == lock_key,
            ReconcileLock.expires_at > now,
        )
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            return False

        # Try to create the lock
        try:
            lock = ReconcileLock(
                resource_type="workspace_reconciler",
                resource_id=lock_key,
                locked_by="WorkspaceReconciler",
                expires_at=expires_at,
            )
            session.add(lock)
            await session.flush()
            return True
        except Exception:
            await session.rollback()
            return False

    async def _release_lock(self, session: AsyncSession, lock_key: str) -> None:
        """Release a reconcile lock."""
        try:
            await session.execute(
                delete(ReconcileLock).where(
                    ReconcileLock.resource_type == "workspace_reconciler",
                    ReconcileLock.resource_id == lock_key,
                )
            )
            await session.commit()
        except Exception:
            logger.exception("Failed to release lock %s", lock_key)
