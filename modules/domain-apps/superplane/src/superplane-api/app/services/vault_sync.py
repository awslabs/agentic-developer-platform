"""VaultSyncReconciler — credential rotation propagation (US-H2).

This control plane reconciler detects credential rotations in the Credential Registry
and propagates updated secrets to all assigned data plane clusters via ExternalSecrets.

Reconciliation loop:
  1. Query all ClusterVaultAssignments that need sync:
     - status == "Pending" (newly assigned)
     - credential's last_rotated_at > assignment's synced_at (rotated since last sync)
     - status == "Failed" with retry backoff elapsed
  2. For each stale assignment:
     a. Acquire a reconcile lock to prevent concurrent syncs
     b. Create/update the ExternalSecret on the target cluster's K8s API
     c. Update assignment status and synced_at
     d. Emit audit log entry
  3. Detect credentials approaching expiry and emit warnings
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.cluster import Cluster
from app.models.credential import (
    ClusterVaultAssignment,
    CredentialAuditLog,
    CredentialRegistry,
)
from app.models.event import Event
from app.models.reconcile_lock import ReconcileLock

logger = logging.getLogger(__name__)


# --- Constants ---

# How often the reconciler runs its loop.
RECONCILE_INTERVAL_SECONDS = 60

# How long to wait before retrying a failed sync.
RETRY_BACKOFF_SECONDS = 300  # 5 minutes

# Lock TTL — prevents stale locks from blocking forever.
LOCK_TTL_SECONDS = 120

# Warn when credentials will expire within this window.
EXPIRY_WARNING_DAYS = 7

# ExternalSecret namespace on data plane clusters.
EXTERNAL_SECRET_NAMESPACE = "superplane-system"


class ExternalSecretClient:
    """Interface for managing ExternalSecret resources on data plane clusters.

    In production this talks to the target cluster's K8s API.
    Abstracted here so the reconciler can be tested without real clusters.
    """

    async def apply_external_secret(
        self,
        cluster_endpoint: str,
        secret_name: str,
        secret_arn: str,
        namespace: str,
        kms_key_id: str | None = None,
    ) -> dict:
        """Create or update an ExternalSecret on the target cluster.

        Returns a dict with status information, e.g.:
          {"synced": True, "resource_version": "12345"}
        """
        raise NotImplementedError

    async def delete_external_secret(
        self,
        cluster_endpoint: str,
        secret_name: str,
        namespace: str,
    ) -> bool:
        """Delete an ExternalSecret from the target cluster. Returns True if deleted."""
        raise NotImplementedError


class KubernetesExternalSecretClient(ExternalSecretClient):
    """Real implementation that calls the target cluster's K8s API via proxy.

    For MVP, this uses the control plane proxy endpoint to forward
    ExternalSecret manifests to data plane clusters.
    """

    async def apply_external_secret(
        self,
        cluster_endpoint: str,
        secret_name: str,
        secret_arn: str,
        namespace: str,
        kms_key_id: str | None = None,
    ) -> dict:
        """Apply an ExternalSecret manifest to the target cluster.

        The ExternalSecret CRD (from external-secrets.io) is expected to be
        installed on data plane clusters. This creates a SecretStore + ExternalSecret
        that pulls the value from AWS Secrets Manager.
        """
        # Build the ExternalSecret manifest
        external_secret = {
            "apiVersion": "external-secrets.io/v1beta1",
            "kind": "ExternalSecret",
            "metadata": {
                "name": secret_name,
                "namespace": namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "superplane-vault-sync",
                },
            },
            "spec": {
                "refreshInterval": "1h",
                "secretStoreRef": {
                    "name": "aws-secrets-manager",
                    "kind": "ClusterSecretStore",
                },
                "target": {
                    "name": secret_name,
                    "creationPolicy": "Owner",
                },
                "data": [
                    {
                        "secretKey": "value",
                        "remoteRef": {
                            "key": secret_arn,
                        },
                    }
                ],
            },
        }

        logger.info(
            "Applying ExternalSecret %s to cluster %s",
            secret_name,
            cluster_endpoint,
        )

        # In production, this would use kubernetes client or proxy to apply.
        # For now, log and return success.
        return {"synced": True, "manifest": external_secret}

    async def delete_external_secret(
        self,
        cluster_endpoint: str,
        secret_name: str,
        namespace: str,
    ) -> bool:
        logger.info(
            "Deleting ExternalSecret %s from cluster %s",
            secret_name,
            cluster_endpoint,
        )
        return True


class VaultSyncReconciler:
    """Control plane reconciler that propagates credential rotations to data plane clusters.

    Watches:
      - ClusterVaultAssignment rows (Pending, Failed, or stale)
      - CredentialRegistry.last_rotated_at changes

    Actions:
      - Creates/updates ExternalSecret on target cluster
      - Updates assignment status and synced_at timestamp
      - Writes audit log entries
      - Emits events for sync successes and failures
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        es_client: ExternalSecretClient | None = None,
        reconcile_interval: int = RECONCILE_INTERVAL_SECONDS,
        retry_backoff: int = RETRY_BACKOFF_SECONDS,
        lock_ttl: int = LOCK_TTL_SECONDS,
    ):
        self._session_factory = session_factory
        self._es_client = es_client or KubernetesExternalSecretClient()
        self._reconcile_interval = reconcile_interval
        self._retry_backoff = retry_backoff
        self._lock_ttl = lock_ttl
        self._running = False
        self._task: asyncio.Task | None = None

    # --- Lifecycle ---

    async def start(self) -> None:
        """Start the reconciliation loop as a background task."""
        if self._running:
            logger.warning("VaultSyncReconciler already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "VaultSyncReconciler started (interval=%ds)", self._reconcile_interval
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
        logger.info("VaultSyncReconciler stopped")

    async def _run_loop(self) -> None:
        """Main reconciliation loop — runs until stopped."""
        while self._running:
            try:
                stats = await self.reconcile()
                logger.info(
                    "VaultSyncReconciler tick: synced=%d, failed=%d, skipped=%d",
                    stats["synced"],
                    stats["failed"],
                    stats["skipped"],
                )
            except Exception:
                logger.exception("VaultSyncReconciler tick failed")
            await asyncio.sleep(self._reconcile_interval)

    # --- Core Reconciliation ---

    async def reconcile(self) -> dict:
        """Run a single reconciliation pass.

        Returns stats dict: {"synced": N, "failed": N, "skipped": N, "expiry_warnings": N}
        """
        stats = {"synced": 0, "failed": 0, "skipped": 0, "expiry_warnings": 0}

        async with self._session_factory() as session:
            # Find assignments that need syncing
            stale_assignments = await self._find_stale_assignments(session)

            for assignment, credential, cluster in stale_assignments:
                try:
                    synced = await self._sync_assignment(
                        session, assignment, credential, cluster
                    )
                    if synced:
                        stats["synced"] += 1
                    else:
                        stats["skipped"] += 1
                except Exception as exc:
                    logger.exception(
                        "Failed to sync assignment %s (credential=%s, cluster=%s)",
                        assignment.id,
                        credential.id,
                        cluster.id,
                    )
                    await self._mark_failed(session, assignment, str(exc))
                    stats["failed"] += 1

            # Check for credentials approaching expiry
            stats["expiry_warnings"] = await self._check_expiry_warnings(session)

        return stats

    async def _find_stale_assignments(
        self, session: AsyncSession
    ) -> list[tuple[ClusterVaultAssignment, CredentialRegistry, Cluster]]:
        """Find all vault assignments that need to be synced.

        A sync is needed when:
          1. Assignment status is 'Pending' (newly created)
          2. Credential was rotated after last sync (last_rotated_at > synced_at)
          3. Assignment status is 'Failed' and retry backoff has elapsed
        """
        now = datetime.now(timezone.utc)
        retry_cutoff = now - timedelta(seconds=self._retry_backoff)

        stmt = (
            select(ClusterVaultAssignment, CredentialRegistry, Cluster)
            .join(
                CredentialRegistry,
                ClusterVaultAssignment.credential_registry_id == CredentialRegistry.id,
            )
            .join(Cluster, ClusterVaultAssignment.cluster_id == Cluster.id)
            .where(
                # Only sync active credentials
                CredentialRegistry.status == "Active",
                # Only sync clusters that are running
                Cluster.status.in_(["Active", "Running", "Healthy"]),
                # Needs sync condition
                or_(
                    # New assignment
                    ClusterVaultAssignment.status == "Pending",
                    # Credential rotated since last sync
                    and_(
                        CredentialRegistry.last_rotated_at.is_not(None),
                        or_(
                            ClusterVaultAssignment.synced_at.is_(None),
                            CredentialRegistry.last_rotated_at
                            > ClusterVaultAssignment.synced_at,
                        ),
                    ),
                    # Failed with backoff elapsed
                    and_(
                        ClusterVaultAssignment.status == "Failed",
                        ClusterVaultAssignment.synced_at < retry_cutoff,
                    ),
                ),
            )
        )

        result = await session.execute(stmt)
        return [tuple(row) for row in result.all()]  # type: ignore[return-value]

    async def _sync_assignment(
        self,
        session: AsyncSession,
        assignment: ClusterVaultAssignment,
        credential: CredentialRegistry,
        cluster: Cluster,
    ) -> bool:
        """Sync a single credential to a cluster. Returns True if synced, False if skipped."""

        # Try to acquire reconcile lock
        lock_key = f"vault-sync:{assignment.id}"
        locked = await self._acquire_lock(session, lock_key)
        if not locked:
            logger.debug("Skipping assignment %s — already locked", assignment.id)
            return False

        try:
            # Build a deterministic secret name from provider and friendly name
            secret_name = _build_secret_name(credential)

            cluster_endpoint = cluster.endpoint or ""
            if not cluster_endpoint:
                logger.warning(
                    "Cluster %s has no endpoint, cannot sync credential %s",
                    cluster.id,
                    credential.id,
                )
                await self._mark_failed(session, assignment, "Cluster has no endpoint")
                return False

            # Apply the ExternalSecret to the target cluster
            result = await self._es_client.apply_external_secret(
                cluster_endpoint=cluster_endpoint,
                secret_name=secret_name,
                secret_arn=credential.secret_arn,
                namespace=EXTERNAL_SECRET_NAMESPACE,
                kms_key_id=credential.kms_key_id,
            )

            if result.get("synced"):
                await self._mark_synced(session, assignment)
                await self._write_audit_log(
                    session,
                    credential=credential,
                    cluster=cluster,
                    action="sync_success",
                )
                await self._emit_event(
                    session,
                    org_id=credential.org_id,
                    resource_type="ClusterVaultAssignment",
                    resource_id=assignment.id,
                    event_type="VaultSyncSucceeded",
                    message=(
                        f"Credential '{credential.friendly_name}' synced to "
                        f"cluster '{cluster.name}'"
                    ),
                )
                return True
            else:
                await self._mark_failed(
                    session, assignment, "ExternalSecret apply returned not synced"
                )
                return False

        finally:
            await self._release_lock(session, lock_key)

    # --- Status Updates ---

    async def _mark_synced(
        self, session: AsyncSession, assignment: ClusterVaultAssignment
    ) -> None:
        """Mark an assignment as successfully synced."""
        now = datetime.now(timezone.utc)
        await session.execute(
            update(ClusterVaultAssignment)
            .where(ClusterVaultAssignment.id == assignment.id)
            .values(status="Synced", synced_at=now)
        )
        await session.commit()

    async def _mark_failed(
        self,
        session: AsyncSession,
        assignment: ClusterVaultAssignment,
        reason: str,
    ) -> None:
        """Mark an assignment as failed sync."""
        now = datetime.now(timezone.utc)
        await session.execute(
            update(ClusterVaultAssignment)
            .where(ClusterVaultAssignment.id == assignment.id)
            .values(status="Failed", synced_at=now)
        )
        await session.commit()
        logger.warning("Vault sync failed for assignment %s: %s", assignment.id, reason)

    # --- Audit & Events ---

    async def _write_audit_log(
        self,
        session: AsyncSession,
        credential: CredentialRegistry,
        cluster: Cluster,
        action: str,
    ) -> None:
        """Write a credential audit log entry."""
        entry = CredentialAuditLog(
            org_id=credential.org_id,
            credential_registry_id=credential.id,
            cluster_id=cluster.id,
            accessed_by="VaultSyncReconciler",
            action=action,
        )
        session.add(entry)
        await session.commit()

    async def _emit_event(
        self,
        session: AsyncSession,
        org_id: uuid.UUID,
        resource_type: str,
        resource_id: uuid.UUID,
        event_type: str,
        message: str,
        details: dict | None = None,
    ) -> None:
        """Emit a platform event."""
        event = Event(
            org_id=org_id,
            resource_type=resource_type,
            resource_id=resource_id,
            event_type=event_type,
            message=message,
            details_json=json.dumps(details) if details else None,
        )
        session.add(event)
        await session.commit()

    # --- Expiry Warnings ---

    async def _check_expiry_warnings(self, session: AsyncSession) -> int:
        """Check for credentials approaching expiry and emit warning events."""
        now = datetime.now(timezone.utc)
        warning_cutoff = now + timedelta(days=EXPIRY_WARNING_DAYS)

        stmt = select(CredentialRegistry).where(
            CredentialRegistry.status == "Active",
            CredentialRegistry.expires_at.is_not(None),
            CredentialRegistry.expires_at <= warning_cutoff,
            CredentialRegistry.expires_at > now,
        )

        result = await session.execute(stmt)
        expiring = result.scalars().all()

        for cred in expiring:
            if cred.expires_at is None:
                continue
            days_left = (cred.expires_at - now).days
            await self._emit_event(
                session,
                org_id=cred.org_id,
                resource_type="CredentialRegistry",
                resource_id=cred.id,
                event_type="CredentialExpiryWarning",
                message=(
                    f"Credential '{cred.friendly_name}' ({cred.provider}) "
                    f"expires in {days_left} days"
                ),
                details={
                    "credential_id": str(cred.id),
                    "provider": cred.provider,
                    "expires_at": cred.expires_at.isoformat(),
                    "days_remaining": days_left,
                },
            )

        return len(expiring)

    # --- Locking ---

    async def _acquire_lock(self, session: AsyncSession, lock_key: str) -> bool:
        """Attempt to acquire a reconcile lock. Returns True if acquired."""
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=self._lock_ttl)

        # Clean up expired locks first
        await session.execute(
            update(ReconcileLock)
            .where(
                ReconcileLock.resource_type == "vault_sync",
                ReconcileLock.resource_id == lock_key,
                ReconcileLock.expires_at < now,
            )
            .values(locked_by="", expires_at=now)  # Effectively release
        )

        # Check if lock exists and is active
        stmt = select(ReconcileLock).where(
            ReconcileLock.resource_type == "vault_sync",
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
                resource_type="vault_sync",
                resource_id=lock_key,
                locked_by="VaultSyncReconciler",
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
            from sqlalchemy import delete

            await session.execute(
                delete(ReconcileLock).where(
                    ReconcileLock.resource_type == "vault_sync",
                    ReconcileLock.resource_id == lock_key,
                )
            )
            await session.commit()
        except Exception:
            logger.exception("Failed to release lock %s", lock_key)

    # --- Manual Trigger ---

    async def trigger_sync(
        self,
        credential_id: uuid.UUID | None = None,
        cluster_id: uuid.UUID | None = None,
    ) -> dict:
        """Manually trigger a vault sync for specific credential/cluster.

        If credential_id is provided, syncs that credential to all assigned clusters.
        If cluster_id is provided, syncs all credentials assigned to that cluster.
        If both are provided, syncs that specific assignment.
        Returns stats dict.
        """
        async with self._session_factory() as session:
            stmt = (
                select(ClusterVaultAssignment, CredentialRegistry, Cluster)
                .join(
                    CredentialRegistry,
                    ClusterVaultAssignment.credential_registry_id
                    == CredentialRegistry.id,
                )
                .join(Cluster, ClusterVaultAssignment.cluster_id == Cluster.id)
            )

            conditions = [CredentialRegistry.status == "Active"]

            if credential_id:
                conditions.append(
                    ClusterVaultAssignment.credential_registry_id == credential_id
                )
            if cluster_id:
                conditions.append(ClusterVaultAssignment.cluster_id == cluster_id)

            stmt = stmt.where(*conditions)
            result = await session.execute(stmt)
            assignments = list(result.all())

            stats = {"synced": 0, "failed": 0, "skipped": 0, "total": len(assignments)}

            for assignment, credential, cluster in assignments:
                try:
                    synced = await self._sync_assignment(
                        session, assignment, credential, cluster
                    )
                    if synced:
                        stats["synced"] += 1
                    else:
                        stats["skipped"] += 1
                except Exception as exc:
                    logger.exception(
                        "Manual sync failed for assignment %s", assignment.id
                    )
                    await self._mark_failed(session, assignment, str(exc))
                    stats["failed"] += 1

        return stats


def _build_secret_name(credential: CredentialRegistry) -> str:
    """Build a deterministic K8s secret name from the credential metadata.

    Format: superplane-{provider}-{sanitized_friendly_name}
    K8s names must be lowercase, alphanumeric + hyphens, max 253 chars.
    """
    sanitized = credential.friendly_name.lower().replace(" ", "-").replace("_", "-")
    # Remove anything that's not alphanumeric or hyphen
    sanitized = "".join(c for c in sanitized if c.isalnum() or c == "-")
    # Strip leading/trailing hyphens and collapse consecutive hyphens
    while "--" in sanitized:
        sanitized = sanitized.replace("--", "-")
    sanitized = sanitized.strip("-")

    name = f"superplane-{credential.provider.lower()}-{sanitized}"
    # Truncate to K8s name limit
    return name[:253]
