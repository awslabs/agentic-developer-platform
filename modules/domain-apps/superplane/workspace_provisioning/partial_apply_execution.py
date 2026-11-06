"""Select only the repair artifact named by an immutable protected retry receipt."""

import json

from harness_jobs.provider_retry import (
    active_call_rows,
    partial_repair_identity,
    receipt_for,
)

from .artifacts import digest
from .partial_apply_artifact import repair_artifact
from .partial_apply_plan import EIP, require
from .terraform import operation_directory


async def selected_repair(operation, context, *, fresh=True):
    lease = operation.grant.lease
    async with context.connect() as connection:
        receipt = await receipt_for(connection, lease.operation_id)
        if receipt is None or receipt["recovery_scope"] != "partial-apply-repair":
            return None
        await active_call_rows(connection, lease.operation_id, [])
        source = await connection.fetchrow(
            "SELECT * FROM harness_operations WHERE operation_id=$1", lease.operation_id
        )
    require(
        source is not None
        and all(
            receipt[k] == source[k]
            for k in ("operation_id", "org_id", "workspace_id", "plan_digest")
        )
        and receipt["source_fence"] < lease.fence_token
        and operation.plan_digest == receipt["plan_digest"]
        and source["version"] >= receipt["restored_version"],
        "repair receipt differs from current original admission",
    )
    proof = json.loads(receipt["proof"])
    review = partial_repair_identity(proof)
    require(
        proof["owner_evidence"]["original_artifact_id"]
        == operation.request.parameters.get("lifecycle_artifact_id"),
        "repair receipt lost original saved-plan identity",
    )
    row, actual = await repair_artifact(
        context.connect,
        context.domain_connect,
        source=source,
        artifact_id=review["artifact_id"],
        evidence=proof["owner_evidence"],
        fresh=fresh,
    )
    require(actual == review, "receipt names a different repair proposal")
    return dict(receipt), row


def repair_directory(operation, context, receipt, call):
    require(
        call.idempotency_key == receipt["replacement_key"],
        "repair call is not receipt-bound",
    )
    lease = operation.grant.lease
    original = operation_directory(
        context.state_root, lease.org_id, lease.workspace_id, lease.operation_id
    )
    directory = original / (
        "repair-" + digest([receipt["reauthorization_id"], call.idempotency_key])
    )
    # Never reuse a directory after an uncertain repair or modify the old apply files.
    directory.mkdir(mode=0o700)
    return directory


def verify_completed_inventory(metadata, snapshot):
    """State after apply must retain every identity except the explicitly replaced EIP."""
    rows = metadata["applied_resources"]
    actual = {row["address"]: row for row in rows}
    require(len(actual) == len(rows), "repair inventory is ambiguous")
    retained = snapshot["resources"]
    for address, previous in retained.items():
        if address == EIP:
            continue
        require(
            address in actual
            and actual[address]["type"] == previous["type"]
            and actual[address]["identity"]["id"] == previous["id"],
            "repair lost a retained infrastructure identity",
        )
    require(
        EIP in actual and actual[EIP]["identity"]["id"] != retained[EIP]["id"],
        "tainted EIP was not replaced as approved",
    )


async def result_source(operation, context, source, metadata):
    """Verify the exact completed repair while keeping the original lineage field."""
    if not metadata.get("repair_artifact_id"):
        return source
    selected = await selected_repair(operation, context, fresh=False)
    require(selected is not None, "repair result has no immutable execution receipt")
    receipt, row = selected
    proof = json.loads(receipt["proof"])
    require(
        metadata["repair_artifact_id"] == row["artifact_id"]
        and metadata.get("partial_repair_receipt") == receipt["reauthorization_id"]
        and metadata.get("source_artifact_id") == source["artifact_id"]
        and metadata.get("replaced_eip_absent")
        == proof["owner_evidence"]["snapshot"]["resources"][EIP]["id"],
        "repair result differs from its approved plan or replaced allocation",
    )
    verify_completed_inventory(metadata, proof["owner_evidence"]["snapshot"])
    return row
