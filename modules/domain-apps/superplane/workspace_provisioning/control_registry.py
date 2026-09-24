"""Explicit registration for paid controls that retain the original bootstrap.

The API owns the workspace transaction and human approval. Registration rereads
the committed shared admission; a crash between admission and registration does
not make an otherwise unregistered provision operation eligible for dispatch.
"""

import json
import uuid

from harness_jobs.allocation import allocation_id_for
from harness_jobs.identity import decode_payload, encode_payload, payload_digest
from harness_jobs.store import _record

from .retirement_access_authority import FIELDS, execution_steps, request_revision
from .retirement_access_plan import PHASE, access_identity
from .runtime_config import LifecycleRefused


async def registration_values(
    connection,
    *,
    operation_id,
    org_id,
    workspace_id,
    source_bootstrap_operation_id,
    request_id,
):
    """Reconstruct registration solely from its original paid admission and source."""
    rows = await connection.fetch(
        "SELECT * FROM harness_operations WHERE operation_id=ANY($1::text[]) AND org_id=$2 AND workspace_id=$3",
        [operation_id, source_bootstrap_operation_id],
        org_id,
        workspace_id,
    )
    records = {row["operation_id"]: _record(row) for row in rows}
    operation, source = (
        records.get(operation_id),
        records.get(source_bootstrap_operation_id),
    )
    if operation is None or source is None or source.state != "succeeded":
        raise LifecycleRefused(
            "cleanup control requires its original successful bootstrap"
        )
    request, original = operation.admitted_request(), source.admitted_request()
    parameters, previous = request.parameters, original.parameters
    try:
        retirement_id = str(uuid.UUID(str(request_id)))
        derived_request, derived_allocation = access_identity(
            org_id, workspace_id, allocation_id_for(source), retirement_id
        )
        source_request = json.loads(previous["lifecycle_request"])
        source_account = source_request["target_account_id"]
        runtime = int(parameters["max_runtime_seconds"])
        valid = (
            request.action == "provision"
            and original.action == "provision"
            and previous.get("lifecycle_phase") == "bootstrap-workspace"
            and set(parameters) == FIELDS
            and parameters["lifecycle_phase"] == PHASE
            and request.idempotency_key == derived_request
            and parameters["retirement_request_id"] == retirement_id
            and parameters["allocation_id"] == derived_allocation
            and parameters["original_allocation_id"] == allocation_id_for(source)
            and parameters["retirement_source_operation_id"] == source.operation_id
            and parameters["retirement_source_job_id"] == source.job_id
            and parameters["retirement_source_attempt_id"] == source.attempt_id
            and parameters["retirement_source_payload_digest"] == source.plan_digest
            and parameters["lifecycle_artifact_id"] == previous["lifecycle_artifact_id"]
            and parameters["lifecycle_request"] == previous["lifecycle_request"]
            and parameters["lifecycle_inputs"] == previous["lifecycle_inputs"]
            and source_request["mode"] == "bring-existing-cluster"
            and source_request["workspace_id"] == workspace_id
            and parameters["provider"] == "aws"
            and parameters["aws_account_id"]
            == parameters["provider_account_id"]
            == previous["aws_account_id"]
            == source_account
            and parameters["region"] == source_request["region"]
            and parameters["max_resource_units"] == parameters["max_cost_micros"] == "0"
            and 0 < runtime <= 86400
            and str(runtime) == parameters["max_runtime_seconds"]
            and parameters["plan_revision"] == request_revision(parameters)
            and parameters["execution_steps"]
            == execution_steps(parameters["retirement_access_recipe_sha256"])
            and encode_payload(request) == operation.request_payload
            and payload_digest(decode_payload(source.request_payload))
            == source.plan_digest
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise LifecycleRefused(
            "cleanup control changed its approved request or bootstrap source"
        )
    paid = await connection.fetchval(
        "SELECT EXISTS(SELECT 1 FROM harness_approval_consumption WHERE operation_id=$1 "
        "AND org_id=$2 AND workspace_id=$3 AND plan_digest=$4 "
        "AND reservation_state IN ('confirmed','retained','released') "
        "AND max_resource_units=0 AND max_cost_micros=0 AND max_runtime_seconds=$5)",
        operation_id,
        org_id,
        workspace_id,
        operation.plan_digest,
        runtime,
    )
    source_paid = await connection.fetchval(
        "SELECT EXISTS(SELECT 1 FROM harness_approval_consumption WHERE operation_id=$1 "
        "AND org_id=$2 AND workspace_id=$3 AND plan_digest=$4 "
        "AND reservation_state IN ('confirmed','retained','released'))",
        source.operation_id,
        org_id,
        workspace_id,
        source.plan_digest,
    )
    registered = await connection.fetchval(
        "SELECT EXISTS(SELECT 1 FROM workspaces WHERE id::text=$1 AND org_id::text=$2 "
        "AND provisioning_operation_id=$3 AND status IN ('Active','active') AND is_default=false)",
        workspace_id,
        org_id,
        source_bootstrap_operation_id,
    )
    if not paid or not source_paid or not registered:
        raise LifecycleRefused(
            "cleanup control paid identity or current bootstrap registration changed"
        )
    return {
        "operation_id": operation_id,
        "org_id": org_id,
        "workspace_id": workspace_id,
        "source_bootstrap_operation_id": source_bootstrap_operation_id,
        "phase": PHASE,
        "request_id": retirement_id,
        "allocation_id": derived_allocation,
        "original_allocation_id": allocation_id_for(source),
        "plan_digest": operation.plan_digest,
    }


async def register_control_operation(connection, **identity):
    """Call after shared admission commits, while the API holds its workspace lock.

    request_id is the original retirement UUID, not the derived admitted request.
    The caller owns this connection's transaction and the domain workspace lock.
    """
    values = await registration_values(connection, **identity)
    await connection.execute(
        "INSERT INTO workspace_lifecycle_control_operations ("
        + ",".join(values)
        + ") VALUES ("
        + ",".join("$" + str(index) for index in range(1, len(values) + 1))
        + ") ON CONFLICT DO NOTHING",
        *values.values(),
    )
    row = await connection.fetchrow(
        "SELECT * FROM workspace_lifecycle_control_operations WHERE operation_id=$1",
        identity["operation_id"],
    )
    if row is None or any(row[key] != value for key, value in values.items()):
        raise LifecycleRefused(
            "cleanup control registration was already bound differently"
        )
    return dict(row)


async def validate_control_operation(connection, operation):
    """A registered control must still match its paid source, scope and allocation."""
    lease = operation.grant.lease
    parameters = operation.request.parameters
    row = await connection.fetchrow(
        "SELECT * FROM workspace_lifecycle_control_operations WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
        lease.operation_id,
        lease.org_id,
        lease.workspace_id,
    )
    if row is None:
        raise LifecycleRefused("cleanup control operation is not registered")
    values = await registration_values(
        connection,
        operation_id=lease.operation_id,
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
        source_bootstrap_operation_id=parameters["retirement_source_operation_id"],
        request_id=parameters["retirement_request_id"],
    )
    if operation.plan_digest != values["plan_digest"] or any(
        row[key] != value for key, value in values.items()
    ):
        raise LifecycleRefused("cleanup control registration changed")
    return dict(row)
