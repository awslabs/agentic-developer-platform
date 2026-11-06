"""Bind a genuinely completed preparation to the original paid partial apply."""

import json

from harness_jobs.identity import decode_payload

from .artifacts import digest, read_artifact
from .partial_apply_plan import require


async def repair_artifact(
    connect, domain_connect, *, source, artifact_id, evidence, fresh=True
):
    row = await read_artifact(
        domain_connect,
        artifact_id=artifact_id,
        org_id=source["org_id"],
        workspace_id=source["workspace_id"],
        require_fresh=fresh,
    )
    async with connect() as connection:
        prep = await connection.fetchrow(
            "SELECT * FROM harness_operations WHERE operation_id=$1",
            row["source_operation_id"],
        )
    require(
        prep is not None
        and prep["state"] == "succeeded"
        and prep["request_payload"] == row["source_request_payload"]
        and all(prep[k] == row["source_" + k] for k in ("job_id", "attempt_id"))
        and prep["plan_digest"] == row["source_payload_digest"]
        and prep["org_id"] == source["org_id"]
        and prep["workspace_id"] == source["workspace_id"],
        "repair proposal must have a completed original preparation",
    )
    async with domain_connect() as domain:
        registration = await domain.fetchrow(
            "SELECT * FROM workspace_partial_apply_preparations WHERE operation_id=$1",
            prep["operation_id"],
        )
    require(
        registration is not None
        and registration["source_operation_id"] == source["operation_id"]
        and registration["plan_digest"] == prep["plan_digest"]
        and json.loads(registration["owner_evidence_json"]) == evidence
        and registration["owner_evidence_sha256"] == digest(evidence),
        "repair preparation evidence changed",
    )
    p = decode_payload(prep["request_payload"]).parameters
    metadata = json.loads(row["artifact_metadata_json"])
    repair = metadata.get("partial_repair", {})
    require(
        p.get("lifecycle_phase") == "prepare-partial-apply"
        and p.get("partial_apply_source_operation_id") == source["operation_id"]
        and p.get("partial_apply_source_plan_digest") == source["plan_digest"]
        and p.get("partial_apply_owner_evidence_sha256") == digest(evidence)
        and metadata.get("next_phase") == "partial-apply-repair"
        and repair.get("snapshot") == evidence["snapshot"]
        and repair.get("source_operation_id") == source["operation_id"]
        and repair.get("source_artifact_id") == evidence["original_artifact_id"]
        and repair.get("owner_evidence_id") == evidence["record_id"]
        and repair.get("owner_evidence_sha256") == digest(evidence),
        "repair proposal changed original lineage",
    )
    result = {
        "artifact_id": artifact_id,
        "preparation_operation_id": prep["operation_id"],
        "plan_file_sha256": metadata["plan_file_sha256"],
        "plan_json_sha256": metadata["plan_json_sha256"],
        "snapshot_sha256": digest(evidence["snapshot"]),
        "changes": repair["changes"],
    }
    from harness_jobs.provider_retry import validate_partial_repair_plan

    validate_partial_repair_plan(evidence, result)
    return row, result
