"""Bind workload admission to its own durable intent, never lifecycle pointers."""

import json

from harness_jobs.identity import OperationRefused, decode_payload, payload_digest

from .deployment_plan import (
    BATCH_FIELDS,
    deployment_identity,
    document_digest,
    teardown_request,
    validate_request,
)

from .network_probe_contract import COMMAND as PROBE_COMMAND

REGISTRY_TABLE = "controller_deployment_operations"


async def registration_values(
    connection, *, operation_id, org_id, workspace_id, deployment_id
):
    row = await connection.fetchrow(
        "SELECT o.*,a.approval_id,a.max_resource_units,a.max_runtime_seconds,a.max_cost_micros "
        "FROM harness_operations o JOIN harness_approval_consumption a ON a.operation_id=o.operation_id "
        "AND a.org_id=o.org_id AND a.workspace_id=o.workspace_id AND a.plan_digest=o.plan_digest "
        "JOIN operation_budget_reservations r ON r.reservation_id=a.reservation_id "
        "AND r.org_id=o.org_id AND r.workspace_id=o.workspace_id AND r.job_id=o.job_id "
        "AND r.attempt_id=o.attempt_id AND r.max_resource_units=a.max_resource_units "
        "AND r.max_runtime_seconds=a.max_runtime_seconds AND r.max_cost_micros=a.max_cost_micros "
        "WHERE o.operation_id=$1 AND o.org_id=$2 AND o.workspace_id=$3 "
        "AND a.reservation_state IN ('confirmed','retained','released') "
        "AND r.state IN ('confirmed','retained','released')",
        operation_id,
        org_id,
        workspace_id,
    )
    intent = await connection.fetchrow(
        "SELECT d.*,w.namespace_name,c.eks_cluster_arn,c.endpoint,c.id::text AS target_cluster_id,"
        "a.account_identifier AS provider_account_id FROM deployments d "
        "JOIN workspaces w ON w.id=d.workspace_id AND w.org_id=d.org_id "
        "JOIN clusters c ON c.id=w.cluster_id AND c.org_id=w.org_id AND c.id=d.cluster_id "
        "JOIN cloud_accounts a ON a.id=w.aws_account_id AND a.org_id=w.org_id "
        "WHERE d.id::text=$1 AND d.org_id::text=$2 AND d.workspace_id::text=$3 "
        "AND w.status IN ('Ready','active') AND c.status IN ('Ready','Active') "
        "AND a.provider='aws' AND a.status='Active'",
        deployment_id,
        org_id,
        workspace_id,
    )
    if row is None or intent is None:
        raise OperationRefused(
            "controller deployment paid admission or canonical intent is unavailable"
        )
    try:
        request = decode_payload(row["request_payload"])
        parameters = request.parameters
        target = json.loads(intent["operation_target_json"])
        document = json.loads(intent["operation_request_json"])
        source_id = parameters.get("controller_source_operation_id", "")
        original_request = decode_payload(intent["controller_request_payload"])
        original_id, allocation_id = deployment_identity(
            org_id, workspace_id, str(intent["operation_id"])
        )
        if (
            payload_digest(request) != row["plan_digest"]
            or request.action != row["action"]
            or parameters["controller_deployment_id"] != deployment_id
            or original_id != deployment_id
            or parameters["allocation_id"] != allocation_id
            or parameters["controller_request_sha256"] != document_digest(document)
            or parameters["controller_target_sha256"] != document_digest(target)
            or intent["namespace"] != intent["namespace_name"]
            or document["name"] != intent["name"]
            or json.loads(parameters["controller_plan"]) != target["controller_plan"]
            or target["controller_plan"]["workload"]["name"] != intent["name"]
            or any(
                target[key] != value
                for key, value in {
                    "cluster_id": intent["target_cluster_id"],
                    "cluster_arn": intent["eks_cluster_arn"],
                    "endpoint": intent["endpoint"],
                    "namespace": intent["namespace_name"],
                    "provider_account_id": intent["provider_account_id"],
                }.items()
            )
            or any(
                str(row[key]) != parameters[key]
                for key in (
                    "max_resource_units",
                    "max_runtime_seconds",
                    "max_cost_micros",
                )
            )
        ):
            raise ValueError("paid intent changed")
        workload = target["controller_plan"]["workload"]
        if workload["kind"] != intent["workload_kind"]:
            raise ValueError("workload lifecycle changed")
        model_fields = (
            "model_name",
            "precision",
            "serving_framework",
            "tensor_parallel_size",
            "max_model_len",
        )
        if workload["kind"] == "serving":
            if document["replicas"] != intent["desired_replicas"] or any(
                document[key] != intent[key]
                for key in (*model_fields, "gpu_per_replica")
            ):
                raise ValueError("serving request changed")
        elif (
            set(document) != BATCH_FIELDS | {"name", "profile_id", "kind"}
            or document["kind"] != "batch"
            or any(
                document[key]
                != (
                    []
                    if key == "args" and workload["command"] == PROBE_COMMAND
                    else workload[key]
                )
                for key in BATCH_FIELDS
            )
            or intent["desired_replicas"] != 1
            or intent["gpu_per_replica"] != workload["gpu_count"]
            or any(intent[key] is not None for key in model_fields)
        ):
            raise ValueError("batch request or quota reservation changed")
        validate_request(request, target, org_id=org_id, workspace_id=workspace_id)
        if request.action == "provision":
            if (
                source_id
                or request.idempotency_key != str(intent["operation_id"])
                or request != original_request
                or intent["controller_approval_id"] != row["approval_id"]
            ):
                raise ValueError("create request changed")
        elif request.action == "teardown":
            from .cleanup_binding import source_for, validate

            source = await source_for(
                connection,
                org_id=org_id,
                workspace_id=workspace_id,
                deployment_id=deployment_id,
                source_id=source_id,
            )
            await validate(
                connection,
                source,
                request,
                approval_id=row["approval_id"],
                require_binding=True,
            )
            original = decode_payload(source["request_payload"])
            if (
                original != original_request
                or payload_digest(original) != source["plan_digest"]
                or teardown_request(
                    original,
                    org_id=org_id,
                    workspace_id=workspace_id,
                    request_id=request.idempotency_key,
                    source_operation_id=source_id,
                )
                != request
            ):
                raise ValueError("teardown changed original allocation or target")
        else:
            raise ValueError("unsupported deployment action")
    except (KeyError, TypeError, ValueError):
        raise OperationRefused(
            "controller deployment registration differs from its original admission"
        ) from None
    return {
        "operation_id": operation_id,
        "deployment_id": deployment_id,
        "org_id": org_id,
        "workspace_id": workspace_id,
        "action": request.action,
        "request_id": request.idempotency_key,
        "allocation_id": allocation_id,
        "source_operation_id": source_id,
        "plan_digest": row["plan_digest"],
        "request_sha256": parameters["controller_request_sha256"],
        "target_sha256": parameters["controller_target_sha256"],
    }


async def register_deployment_operation(connection, **identity):
    """Caller holds canonical workspace/intent locks and owns this transaction.

    Admission has already committed through the shared facade. An orphan outbox
    row cannot dispatch until this exact immutable registration also commits.
    """
    values = await registration_values(connection, **identity)
    await connection.execute(
        "INSERT INTO controller_deployment_operations ("
        + ",".join(values)
        + ") VALUES ("
        + ",".join("$" + str(index) for index in range(1, len(values) + 1))
        + ") ON CONFLICT DO NOTHING",
        *values.values(),
    )
    row = await connection.fetchrow(
        "SELECT * FROM controller_deployment_operations WHERE operation_id=$1",
        identity["operation_id"],
    )
    if row is None or any(row[key] != value for key, value in values.items()):
        raise OperationRefused(
            "controller deployment already has a different registered operation"
        )
    return dict(row)


async def validate_deployment_operation(connection, operation):
    lease = operation.grant.lease
    row = await connection.fetchrow(
        "SELECT * FROM controller_deployment_operations WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
        lease.operation_id,
        lease.org_id,
        lease.workspace_id,
    )
    if row is None:
        raise OperationRefused("controller deployment operation is not registered")
    values = await registration_values(
        connection,
        operation_id=lease.operation_id,
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
        deployment_id=row["deployment_id"],
    )
    if operation.plan_digest != values["plan_digest"] or any(
        row[key] != value for key, value in values.items()
    ):
        raise OperationRefused("controller deployment registration changed")
    return dict(row)


async def require_deployment_registration(connection, operation):
    """V2 production tasks require the workload registry; retain historical V1 reads."""
    parameters = operation.request.parameters
    if "controller_deployment_id" not in parameters:
        try:
            if json.loads(parameters["controller_plan"])["version"] == 1:
                return None
        except (KeyError, TypeError, ValueError):
            pass
        raise OperationRefused(
            "controller workload has no original deployment registration"
        )
    return await validate_deployment_operation(connection, operation)
