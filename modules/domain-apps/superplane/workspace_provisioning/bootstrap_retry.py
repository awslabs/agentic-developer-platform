"""Select a new private directory only from the original immutable retry receipt."""

import json

from harness_jobs.provider_retry import (
    active_call_rows,
    bootstrap_boundary_identity,
    bootstrap_recoverable,
    receipt_for,
)

from .artifacts import digest, read_artifact
from .runtime_config import LifecycleRefused
from .terraform import operation_directory


async def retry_artifact(operation, context):
    """Use only the unchanged completed apply bound by a fresh bootstrap decision.

    This is execution of an already admitted bootstrap, not a new plan review.
    Current execution authority is checked separately before credentials/effects.
    """
    lease = operation.grant.lease
    async with context.connect() as connection:
        receipt = await receipt_for(connection, lease.operation_id)
        if receipt is None or receipt["recovery_scope"] != "unstarted-bootstrap":
            return None
        await active_call_rows(connection, lease.operation_id, [])
        source = await connection.fetchrow(
            "SELECT * FROM harness_operations WHERE operation_id=$1", lease.operation_id
        )
    proof = json.loads(receipt["proof"])
    bootstrap_boundary_identity(proof)
    if (
        source is None
        or operation.request.parameters.get("lifecycle_phase") != "bootstrap-workspace"
        or any(
            receipt[k] != source[k] or receipt[k] != getattr(lease, k)
            for k in ("operation_id", "org_id", "workspace_id")
        )
        or receipt["plan_digest"] != source["plan_digest"]
        or receipt["plan_digest"] != operation.plan_digest
        or receipt["source_fence"] >= lease.fence_token
        or source["version"] < receipt["restored_version"]
        or proof["source_artifact_id"]
        != operation.request.parameters["lifecycle_artifact_id"]
    ):
        raise LifecycleRefused("bootstrap receipt differs from the original admission")
    row = await read_artifact(
        context.domain_connect,
        artifact_id=proof["source_artifact_id"],
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
        require_fresh=False,
    )
    actual = digest(
        {
            k: row[k]
            for k in (
                "artifact_id",
                "source_operation_id",
                "artifact_metadata_json",
                "source_payload_digest",
            )
        }
    )
    if actual != proof["source_artifact_sha256"]:
        raise LifecycleRefused("bootstrap receipt names a different apply artifact")
    return row


async def retry_directory(operation, context, call):
    lease = operation.grant.lease
    async with context.connect() as connection:
        receipt = await receipt_for(connection, lease.operation_id)
        if receipt is not None:
            await active_call_rows(connection, lease.operation_id, [])
    if receipt is None:
        return None
    proof = json.loads(receipt["proof"])
    bootstrap_boundary_identity(proof)
    if (
        receipt["recovery_scope"] != "unstarted-bootstrap"
        or receipt["replacement_key"] != call.idempotency_key
        or receipt["source_fence"] >= lease.fence_token
        or receipt["plan_digest"] != operation.plan_digest
        or any(
            receipt[key] != getattr(lease, key)
            for key in ("org_id", "workspace_id", "operation_id")
        )
        or not bootstrap_recoverable(proof)
        or proof.get("source_artifact_id")
        != operation.request.parameters["lifecycle_artifact_id"]
    ):
        raise LifecycleRefused(
            "bootstrap retry differs from its original approved receipt"
        )
    # Never rename, delete, copy credentials from, or execute anything retained by
    # the original attempt. Exclusive creation still refuses duplicate execution.
    return operation_directory(
        context.state_root,
        lease.org_id,
        lease.workspace_id,
        lease.operation_id + ":bootstrap-retry:" + receipt["reauthorization_id"],
        create=True,
    )
