"""Fresh bootstrap review after genuine recovery of an immutable apply result."""

import json

from .artifacts import read_artifact
from .runtime_config import LifecycleRefused


async def read_continuation_artifact(
    domain_connect,
    operation_connect,
    *,
    artifact_id,
    org_id,
    workspace_id,
    require_fresh=True,
):
    scope = dict(artifact_id=artifact_id, org_id=org_id, workspace_id=workspace_id)
    try:
        return await read_artifact(domain_connect, **scope, require_fresh=require_fresh)
    except LifecycleRefused as exc:
        if str(exc) != "plan proposal expired; prepare a fresh plan":
            raise

    # An old plan remains expired. Only a completed apply's bootstrap handoff
    # can use its later, authenticated recovery observation as freshness proof.
    row = await read_artifact(domain_connect, **scope, require_fresh=False)
    metadata = json.loads(row["artifact_metadata_json"])
    parameters = json.loads(row["parameters_json"])
    if (
        metadata.get("next_phase") != "bootstrap-workspace"
        or parameters.get("lifecycle_phase") != "apply-infrastructure"
        or metadata.get("allocation_source_operation_id") != row["source_operation_id"]
    ):
        raise LifecycleRefused("plan proposal expired; prepare a fresh plan")

    async with operation_connect() as connection:
        verified = await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM harness_operations o "
            "JOIN harness_operation_leases l ON l.operation_id=o.operation_id "
            "AND l.org_id=o.org_id AND l.workspace_id=o.workspace_id "
            "JOIN harness_execution_audit a ON a.operation_id=o.operation_id "
            "AND a.org_id=o.org_id AND a.workspace_id=o.workspace_id "
            "AND a.attempt_id=l.closed_attempt_id "
            "AND a.fence_token=l.fence_token AND a.actor=l.closed_holder "
            "WHERE o.operation_id=$1 AND o.org_id=$2 AND o.workspace_id=$3 "
            "AND o.state='succeeded' AND o.job_id=$4 AND o.attempt_id=$5 "
            "AND o.plan_digest=$6 AND o.request_payload=$7 "
            "AND a.event='recovery.settled' AND a.allowed AND a.detail='succeeded' "
            "AND a.recorded_at=o.updated_at AND a.recorded_at<=l.closed_at "
            "AND a.recorded_at BETWEEN now()-interval '1 hour' AND now())",
            row["source_operation_id"],
            org_id,
            workspace_id,
            row["source_job_id"],
            row["source_attempt_id"],
            row["source_payload_digest"],
            row["source_request_payload"],
        )
    if not verified:
        raise LifecycleRefused("plan proposal expired; prepare a fresh plan")
    # No timestamp, artifact, approval or claim is rewritten. The caller still
    # requires a new exact bootstrap decision and current execution authority.
    return row
