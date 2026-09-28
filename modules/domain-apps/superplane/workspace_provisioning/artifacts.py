"""Exact plan proposals and immutable lineage for separately approved continuation."""

from datetime import UTC, datetime, timedelta
import hashlib
import json
import re

from .execution_contract import ExecutionStep, encode_execution_steps
from .runtime_config import LifecycleRefused


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def initial_execution_steps(parameters):
    request = json.loads(parameters["lifecycle_request"])
    if request["mode"] == "new-account-managed":
        from account_factory.modes import from_mapping
        from account_provisioning.creation_runner import creation_target

        return encode_execution_steps(
            [
                ExecutionStep(
                    "create-account",
                    "aws-organizations",
                    "create-account",
                    creation_target(from_mapping(request)),
                )
            ]
        )
    phase = (
        "prepare-adoption"
        if request["mode"] == "bring-existing-cluster"
        else "prepare-infrastructure"
    )
    return encode_execution_steps(
        [
            ExecutionStep(
                phase, "superplane-lifecycle", phase, parameters["plan_revision"]
            )
        ]
    )


async def record_artifact(operation, context, *, account_id, target, metadata):
    """Commit immutable reviewed bytes' identities while the real grant is locked."""
    from harness_jobs.leases import lock_lease

    lease = operation.grant.lease
    if not isinstance(account_id, str) or not re.fullmatch(r"[0-9]{12}", account_id):
        raise LifecycleRefused("provider account identity is invalid")
    if not isinstance(target, dict) or not isinstance(metadata, dict):
        raise LifecycleRefused("provider proposal facts must be objects")
    parameters = dict(operation.request.parameters)
    values = {
        "org_id": lease.org_id,
        "workspace_id": lease.workspace_id,
        "source_operation_id": lease.operation_id,
        "source_job_id": operation.job_id,
        "producer_holder": lease.holder,
        "producer_attempt_id": lease.attempt_id,
        "producer_fence_token": lease.fence_token,
        "request_revision": parameters["plan_revision"],
        "account_id": account_id,
        "target_json": canonical(target),
        "parameters_json": canonical(parameters),
        "artifact_metadata_json": canonical(metadata),
    }
    async with context.connect() as execution, execution.transaction():
        if not await lock_lease(execution, lease):
            raise LifecycleRefused("plan producer lease is stale")
        source = await execution.fetchrow(
            "SELECT job_id,attempt_id,plan_digest,request_payload FROM harness_operations WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
            lease.operation_id,
            lease.org_id,
            lease.workspace_id,
        )
        if (
            source is None
            or source["job_id"] != operation.job_id
            or source["plan_digest"] != operation.plan_digest
            or source["request_payload"] != operation.request_payload
        ):
            raise LifecycleRefused("plan producer original admission changed")
        values.update(
            source_attempt_id=source["attempt_id"],
            source_payload_digest=source["plan_digest"],
            source_request_payload=source["request_payload"],
        )
        artifact_id = digest(values)
        columns = tuple(values)
        async with context.domain_connect() as domain:
            await domain.execute(
                "INSERT INTO workspace_lifecycle_artifacts (artifact_id,"
                + ",".join(columns)
                + ") VALUES ($1,"
                + ",".join("$" + str(i) for i in range(2, len(columns) + 2))
                + ") ON CONFLICT (artifact_id) DO NOTHING",
                artifact_id,
                *values.values(),
            )
            row = await domain.fetchrow(
                "SELECT * FROM workspace_lifecycle_artifacts WHERE artifact_id=$1",
                artifact_id,
            )
    if row is None or any(row[key] != value for key, value in values.items()):
        raise LifecycleRefused("immutable plan proposal differs")
    from .authority import current_operation, validated_request

    validated_request(await current_operation(operation, context), context)
    return proposal(dict(row))


async def read_artifact(
    connect, *, artifact_id, org_id, workspace_id, require_fresh=True
):
    """Historical ownership reads may disable expiry; approvals must keep it enabled."""
    if not isinstance(artifact_id, str) or not re.fullmatch(
        r"[a-f0-9]{64}", artifact_id
    ):
        raise LifecycleRefused("plan artifact identity is invalid")
    async with connect() as connection:
        row = await connection.fetchrow(
            "SELECT * FROM workspace_lifecycle_artifacts WHERE artifact_id=$1 AND org_id=$2 AND workspace_id=$3",
            artifact_id,
            org_id,
            workspace_id,
        )
    if row is None:
        raise LifecycleRefused("no plan proposal exists for this workspace")
    from harness_jobs.identity import decode_payload, payload_digest

    immutable = {
        key: value
        for key, value in dict(row).items()
        if key not in {"artifact_id", "created_at"}
    }
    request = decode_payload(row["source_request_payload"])
    if (
        digest(immutable) != artifact_id
        or payload_digest(request) != row["source_payload_digest"]
        or canonical(dict(request.parameters)) != row["parameters_json"]
        or row["request_revision"] != request.parameters["plan_revision"]
    ):
        raise LifecycleRefused(
            "recorded plan proposal or original admission digest changed"
        )
    if require_fresh and row["created_at"] < datetime.now(UTC) - timedelta(hours=1):
        raise LifecycleRefused("plan proposal expired; prepare a fresh plan")
    return dict(row)


def proposal(row):
    """Only reviewable facts leave the worker; local paths and credentials do not."""
    metadata = json.loads(row["artifact_metadata_json"])
    parameters = json.loads(row["parameters_json"])
    if metadata["next_phase"] == "complete":
        if parameters.get("lifecycle_phase") != "bootstrap-workspace":
            raise LifecycleRefused("only canonical bootstrap can publish readiness")
        return {
            "status": "ready",
            "workspace_id": row["workspace_id"],
            "operation_id": row["source_operation_id"],
            "lifecycle_artifact_id": parameters["lifecycle_artifact_id"],
            "allocation_source_operation_id": metadata.get(
                "allocation_source_operation_id"
            ),
            "result_artifact_id": row["artifact_id"],
        }
    return {
        "status": "awaiting_plan_approval",
        "artifact_id": row["artifact_id"],
        "source_operation_id": row["source_operation_id"],
        "source_job_id": row["source_job_id"],
        "source_attempt_id": row["source_attempt_id"],
        "workspace_id": row["workspace_id"],
        "request_revision": row["request_revision"],
        "account_id": row["account_id"],
        "target": json.loads(row["target_json"]),
        "phase": metadata["next_phase"],
        "plan_file_sha256": metadata.get("plan_file_sha256"),
        "plan_json_sha256": metadata.get("plan_json_sha256"),
        "inventory": metadata.get("inventory"),
        "estimate": metadata.get("estimate"),
        "lifecycle_request": json.loads(parameters["lifecycle_request"]),
        "lifecycle_inputs": json.loads(parameters["lifecycle_inputs"]),
    }


def continuation_parameters(row):
    """Feed the existing approval producer; never accept worker-selected artifact paths."""
    parameters = json.loads(row["parameters_json"])
    metadata = json.loads(row["artifact_metadata_json"])
    phase = metadata["next_phase"]
    if phase not in {
        "bootstrap-account",
        "prepare-infrastructure",
        "apply-infrastructure",
        "bootstrap-workspace",
    }:
        raise LifecycleRefused("plan continuation phase is invalid")
    request = json.loads(parameters["lifecycle_request"])
    credential_account = (
        request["management_account_id"]
        if request["mode"] == "new-account-managed"
        else row["account_id"]
    )
    parameters.update(
        lifecycle_phase=phase,
        lifecycle_source_operation_id=row["source_operation_id"],
        lifecycle_artifact_id=row["artifact_id"],
        aws_account_id=credential_account,
        execution_steps=encode_execution_steps(
            [ExecutionStep(phase, "superplane-lifecycle", phase, row["artifact_id"])]
        ),
    )
    for key in ("max_resource_units", "max_cost_micros", "max_runtime_seconds"):
        envelope = parameters["lifecycle_allocation_" + key]
        parameters[key] = (
            envelope
            if phase == "apply-infrastructure" or key == "max_runtime_seconds"
            else "0"
        )
    return parameters
