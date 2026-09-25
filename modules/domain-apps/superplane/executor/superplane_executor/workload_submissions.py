"""Immutable pre-POST specification evidence; never a server UID or permission."""

import hashlib
import json

from harness_jobs.identity import OperationRefused
from harness_jobs.leases import lock_lease


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


async def record(provider, operation, call, obj, authorize):
    lease = operation.grant.lease
    document = canonical(obj)
    if len(document.encode()) > 65536:
        raise OperationRefused("submitted workload exceeds evidence bound")
    values = dict(
        operation_id=lease.operation_id,
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
        allocation_id=operation.request.parameters["allocation_id"],
        plan_digest=operation.plan_digest,
        step_key=call.idempotency_key,
        attempt_id=lease.attempt_id,
        fence_token=lease.fence_token,
        kind=obj["kind"],
        namespace=obj["metadata"]["namespace"],
        name=obj["metadata"]["name"],
        body=document,
        body_sha256=hashlib.sha256(document.encode()).hexdigest(),
    )
    if (
        call.operation_id,
        call.org_id,
        call.workspace_id,
        call.attempt_id,
        call.fence_token,
        call.operation_kind,
    ) != (
        lease.operation_id,
        lease.org_id,
        lease.workspace_id,
        lease.attempt_id,
        lease.fence_token,
        "deploy",
    ):
        raise OperationRefused("workload submission dispatch differs")
    await authorize()
    async with provider.execution_pool.acquire() as shared, shared.transaction():
        if not await lock_lease(shared, lease):
            raise OperationRefused("workload submission lease is no longer live")
        async with provider.domain_pool.acquire() as domain, domain.transaction():
            await domain.execute(
                "INSERT INTO controller_workload_submissions "
                "(operation_id,org_id,workspace_id,allocation_id,plan_digest,step_key,attempt_id,fence_token,kind,namespace,name,body,body_sha256) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) ON CONFLICT DO NOTHING",
                *values.values(),
            )
            row = await domain.fetchrow(
                "SELECT * FROM controller_workload_submissions WHERE operation_id=$1 AND kind=$2 AND namespace=$3 AND name=$4",
                lease.operation_id,
                values["kind"],
                values["namespace"],
                values["name"],
            )
            if row is None or any(row[key] != value for key, value in values.items()):
                raise OperationRefused("original workload submission changed")
            await authorize()


async def originals(provider, operation, creating):
    lease = operation.grant.lease
    async with provider.domain_pool.acquire() as domain:
        rows = await domain.fetch(
            "SELECT * FROM controller_workload_submissions WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3 ORDER BY kind,name LIMIT 4",
            lease.operation_id,
            lease.org_id,
            lease.workspace_id,
        )
    if not rows or len(rows) > 2:
        return None
    result = {}
    for row in rows:
        if (
            any(
                row[key] != value
                for key, value in {
                    "allocation_id": operation.request.parameters["allocation_id"],
                    "plan_digest": operation.plan_digest,
                    "step_key": creating["idempotency_key"],
                    "attempt_id": creating["attempt_id"],
                    "fence_token": creating["fence_token"],
                }.items()
            )
            or hashlib.sha256(row["body"].encode()).hexdigest() != row["body_sha256"]
        ):
            return None
        obj = json.loads(row["body"])
        identity = (obj["kind"], obj["metadata"]["namespace"], obj["metadata"]["name"])
        if (
            identity != (row["kind"], row["namespace"], row["name"])
            or identity in result
        ):
            return None
        result[identity] = obj
    return result
