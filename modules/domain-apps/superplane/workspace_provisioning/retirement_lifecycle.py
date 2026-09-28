"""Withdraw admission without deleting the ownership records needed for cleanup."""

import asyncio
import hashlib
import json
from urllib.parse import quote

from harness_jobs.execution import CallOutcome
from harness_jobs.identity import OperationRefused
from harness_jobs.recovery import request_cancellation


class RetirementLifecycle:
    def __init__(
        self,
        *,
        domain_pool,
        execution_pool,
        workspace,
        drain_seconds=30,
        managed_objects=None,
        managed_fence=None,
    ):
        if type(drain_seconds) is not int or not 1 <= drain_seconds <= 60:
            raise ValueError("drain polling must have a bounded deadline")
        self.domain_pool, self.execution_pool = domain_pool, execution_pool
        self.workspace, self.drain_seconds = workspace, drain_seconds
        self.managed_objects = managed_objects
        self.managed_fence = managed_fence

    async def managed_destroy_ready(self, operation, inventory, authorize):
        # Producer-owned immutable tuples (kind, namespace, name, UID). Missing
        # inventory cannot authorize Terraform's cascading EKS destruction.
        if (
            inventory.cluster_ownership != "adp-created"
            or self.managed_objects is None
            or self.managed_fence is None
        ):
            return False
        # The producer verifies its persistent cluster admission interlock,
        # which must remain in force throughout Terraform execution. UID reads
        # alone cannot exclude a concurrent unowned CREATE.
        if not await self.managed_fence(operation, inventory):
            return False
        owned = tuple(sorted(self.managed_objects))
        if any(
            len(item) != 4
            or not all(isinstance(value, str) for value in item)
            or not item[3]
            for item in owned
        ):
            raise OperationRefused("managed workload ownership inventory is malformed")
        digest = hashlib.sha256(
            json.dumps(owned, separators=(",", ":")).encode()
        ).hexdigest()
        if (
            operation.request.parameters.get("managed_workload_inventory_sha256")
            != digest
        ):
            raise OperationRefused("managed workload deletion set was not approved")
        target = {
            "namespace": inventory.namespace,
            "cluster_arn": inventory.cluster_arn,
        }
        async with self.domain_pool.acquire() as connection:
            target["endpoint"] = await connection.fetchval(
                "SELECT c.endpoint FROM clusters c JOIN workspaces w ON w.cluster_id=c.id AND w.org_id=c.org_id "
                "WHERE w.id::text=$1 AND w.org_id::text=$2 AND c.eks_cluster_arn=$3",
                operation.grant.lease.workspace_id,
                operation.grant.lease.org_id,
                inventory.cluster_arn,
            )
        if not target["endpoint"]:
            raise OperationRefused("managed retirement target unavailable")
        for kind, path in (
            ("Pod", "/api/v1/pods"),
            ("PersistentVolumeClaim", "/api/v1/persistentvolumeclaims"),
            ("PersistentVolume", "/api/v1/persistentvolumes"),
            ("Service", "/api/v1/services"),
            ("Job", "/apis/batch/v1/jobs"),
            ("CronJob", "/apis/batch/v1/cronjobs"),
            ("Deployment", "/apis/apps/v1/deployments"),
            ("ReplicaSet", "/apis/apps/v1/replicasets"),
            ("StatefulSet", "/apis/apps/v1/statefulsets"),
            ("DaemonSet", "/apis/apps/v1/daemonsets"),
        ):
            continuation, seen = "", set()
            while True:
                await authorize()
                query = (
                    path
                    + "?limit=100"
                    + (
                        "&continue=" + quote(continuation, safe="")
                        if continuation
                        else ""
                    )
                )
                response = await self.workspace.request(operation, target, "GET", query)
                if response.status_code != 200:
                    return False
                payload = response.json()
                if not isinstance(payload.get("items"), list):
                    return False
                for item in payload["items"]:
                    metadata = item.get("metadata", {})
                    if (
                        kind,
                        metadata.get("namespace", ""),
                        metadata.get("name"),
                        metadata.get("uid"),
                    ) not in owned:
                        return False
                continuation = payload.get("metadata", {}).get("continue", "")
                if not continuation:
                    break
                if (
                    not isinstance(continuation, str)
                    or continuation in seen
                    or len(seen) >= 100
                ):
                    return False
                seen.add(continuation)
        await authorize()
        return await self.managed_fence(operation, inventory)

    async def status(self, operation, inventory, value, authorize):
        if value not in {"Teardown", "retired"}:
            raise OperationRefused("unsupported retirement status")
        await authorize()
        async with self.domain_pool.acquire() as connection:
            result = await connection.fetchval(
                "UPDATE workspaces w SET status=$3 FROM organizations o "
                "WHERE w.org_id=o.id AND w.id::text=$1 AND "
                "(o.id::text=$2 OR o.adp_org_id=$2) AND w.is_default=false "
                "AND w.status IN ('Active','Ready','active','Teardown','retired') RETURNING w.id::text",
                inventory.workspace_id,
                inventory.org_id,
                value,
            )
        if result != inventory.workspace_id:
            raise OperationRefused(
                "workspace cannot enter retirement in original scope"
            )
        await authorize()
        # This unregisters the active routing projection, retaining canonical
        # Workspace/Cluster/bootstrap journals for subsequent deletes and recovery.
        return (
            CallOutcome.SUCCEEDED,
            "workspace admission withdrawn",
            inventory.workspace_id,
        )

    async def drain(self, operation, inventory, authorize):
        lease = operation.grant.lease
        async with self.execution_pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT operation_id FROM harness_operations WHERE org_id=$1 AND workspace_id=$2 "
                "AND operation_id<>$3 AND cancel_requested_at IS NULL "
                "AND state NOT IN ('succeeded','failed','cancelled','unresolved')",
                lease.org_id,
                lease.workspace_id,
                lease.operation_id,
            )
            for row in rows:
                await authorize()
                await request_cancellation(
                    connection,
                    operation_id=row["operation_id"],
                    principal=operation.grant.principal,
                    reason="workspace retirement",
                )
        deadline = asyncio.get_running_loop().time() + self.drain_seconds
        while True:
            await authorize()
            async with self.domain_pool.acquire() as connection:
                target = await connection.fetchrow(
                    "SELECT w.namespace_name AS namespace,c.eks_cluster_arn AS cluster_arn,c.endpoint "
                    "FROM workspaces w JOIN organizations o ON o.id=w.org_id "
                    "JOIN clusters c ON c.id=w.cluster_id AND c.org_id=w.org_id "
                    "WHERE w.id::text=$1 AND (o.id::text=$2 OR o.adp_org_id=$2) "
                    "AND w.status IN ('Teardown','retired')",
                    inventory.workspace_id,
                    inventory.org_id,
                )
                active = await connection.fetchval(
                    "SELECT count(*) FROM controller_capacity WHERE org_id=$1 AND workspace_id=$2 "
                    "AND state<>'retired'",
                    lease.org_id,
                    lease.workspace_id,
                )
            if target is None or (target["namespace"], target["cluster_arn"]) != (
                inventory.namespace,
                inventory.cluster_arn,
            ):
                raise OperationRefused("retirement target changed")
            present = False
            # Query actual governed root objects and pods even when the database
            # has no active row. Unlabelled/adopted workloads are never deleted.
            for kind in ("Job", "Deployment", "Service", "Pod", "Secret"):
                continuation = ""
                seen = set()
                while True:
                    await authorize()
                    path = (
                        self.workspace.path(target, kind)
                        + "?limit=100&labelSelector="
                        + quote("superplane.ai/capacity", safe="")
                    )
                    if continuation:
                        path += "&continue=" + quote(continuation, safe="")
                    response = await self.workspace.request(
                        operation, target, "GET", path
                    )
                    if response.status_code != 200:
                        raise OperationRefused(
                            "governed workload drain cannot be observed"
                        )
                    payload = response.json()
                    if not isinstance(payload.get("items"), list):
                        raise OperationRefused("governed workload list is malformed")
                    present |= bool(payload["items"])
                    continuation = payload.get("metadata", {}).get("continue", "")
                    if not continuation:
                        break
                    if not isinstance(continuation, str) or continuation in seen:
                        raise OperationRefused(
                            "governed workload pagination is incomplete"
                        )
                    seen.add(continuation)
                    if len(seen) > 100:
                        raise OperationRefused(
                            "governed workload inventory exceeds drain bound"
                        )
            await authorize()
            if not active and not present:
                return (
                    CallOutcome.SUCCEEDED,
                    "fresh governed workload absence observed",
                    inventory.workspace_id,
                )
            if asyncio.get_running_loop().time() >= deadline:
                # Cancellation is not teardown. Existing allocated workloads must
                # finish their separately admitted cleanup; never delete by label.
                return (
                    CallOutcome.UNKNOWN,
                    "governed allocation cleanup remains outstanding",
                    inventory.workspace_id,
                )
            await asyncio.sleep(1)
