"""Fixed read-only probe, executed inside the selected API image with JSON arguments."""

import asyncio
import json
import re
import sys
from contextlib import asynccontextmanager, suppress
from datetime import datetime


def require(condition):
    if not condition:
        raise ValueError("ownership observation refused")


async def collect(connect, scope):
    from harness_jobs.identity import decode_payload, payload_digest

    from workspace_provisioning.artifacts import initial_execution_steps, read_artifact

    org, workspace = scope["org_id"], scope["workspace_id"]
    async with connect() as connection:
        transaction = connection.transaction(isolation="repeatable_read", readonly=True)
        async with transaction:

            @asynccontextmanager
            async def snapshot():
                yield connection

            roots = await connection.fetch(
                "SELECT * FROM harness_operations WHERE org_id=$1 AND workspace_id=$2 "
                "AND idempotency_key=$3 LIMIT 2",
                org,
                workspace,
                scope["request_id"],
            )
            require(len(roots) == 1)
            root = roots[0]
            candidates = await connection.fetch(
                "SELECT artifact_id FROM workspace_lifecycle_artifacts "
                "WHERE org_id=$1 AND workspace_id=$2 "
                "AND artifact_metadata_json::jsonb->>'next_phase'='bootstrap-workspace' LIMIT 2",
                org,
                workspace,
            )
            require(len(candidates) == 1)
            applied = None
            child = None
            identity = candidates[0]["artifact_id"]
            for phase in ("apply-infrastructure", "prepare-infrastructure"):
                row = await read_artifact(
                    snapshot,
                    artifact_id=identity,
                    org_id=org,
                    workspace_id=workspace,
                    require_fresh=False,
                )
                require(
                    row["org_id"] == org
                    and row["workspace_id"] == workspace
                    and row["account_id"] == scope["account"]
                    and datetime.fromisoformat(scope["authorized_at"])
                    <= row["created_at"]
                    <= datetime.fromisoformat(scope["observed_at"])
                )
                operation = await connection.fetchrow(
                    "SELECT * FROM harness_operations WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
                    row["source_operation_id"],
                    org,
                    workspace,
                )
                require(operation is not None)
                request = decode_payload(operation["request_payload"])
                parameters = json.loads(row["parameters_json"])
                metadata = json.loads(row["artifact_metadata_json"])
                target = json.loads(row["target_json"])
                require(
                    operation["state"] == "succeeded"
                    and operation["org_id"] == org
                    and operation["workspace_id"] == workspace
                    and operation["operation_id"] == row["source_operation_id"]
                    and operation["job_id"] == row["source_job_id"]
                    and operation["attempt_id"] == row["source_attempt_id"]
                    and payload_digest(request)
                    == operation["plan_digest"]
                    == row["source_payload_digest"]
                    and operation["request_payload"] == row["source_request_payload"]
                    and request.idempotency_key == operation["idempotency_key"]
                    and request.action == "provision"
                    and dict(request.parameters) == parameters
                    and parameters.get("lifecycle_phase", "prepare-infrastructure")
                    == phase
                    and parameters["plan_revision"] == scope["plan_revision"]
                    and all(
                        target[key] == expected
                        for key, expected in {
                            "account_id": scope["account"],
                            "aws_region": scope["region"],
                            "org_id": org,
                            "workspace_id": workspace,
                        }.items()
                    )
                    and json.loads(parameters["lifecycle_request"])["mode"]
                    == "existing-account-managed"
                )
                if child is None:
                    require(
                        metadata["allocation_source_operation_id"]
                        == row["source_operation_id"]
                    )
                    applied = row
                    child = parameters
                    identity = metadata["source_artifact_id"]
                    require(identity == parameters["lifecycle_artifact_id"])
                else:
                    require(
                        row["source_operation_id"] == root["operation_id"]
                        and request.idempotency_key == scope["request_id"]
                        and metadata["next_phase"] == "apply-infrastructure"
                        and child["lifecycle_source_operation_id"]
                        == row["source_operation_id"]
                        and child["lifecycle_request"]
                        == parameters["lifecycle_request"]
                        and child["lifecycle_inputs"] == parameters["lifecycle_inputs"]
                        and applied["created_at"] >= row["created_at"]
                        and applied["target_json"] == row["target_json"]
                        and json.loads(applied["artifact_metadata_json"])[
                            "module_sha256"
                        ]
                        == metadata["module_sha256"]
                        and parameters["execution_steps"]
                        == initial_execution_steps(parameters)
                    )
            return inventory(applied, scope, root["operation_id"])


def inventory(row, scope, original_operation):
    metadata = json.loads(row["artifact_metadata_json"])
    outputs = {key: value["value"] for key, value in metadata["outputs"].items()}
    require(metadata["next_phase"] == "bootstrap-workspace")
    require(
        all(
            outputs[key] == value
            for key, value in {
                "account_id": scope["account"],
                "aws_region": scope["region"],
                "org_id": scope["org_id"],
                "workspace_id": scope["workspace_id"],
            }.items()
        )
    )
    account, region = scope["account"], scope["region"]
    cluster = outputs["cluster_name"]
    require(
        isinstance(cluster, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", cluster)
    )
    cluster_arn = f"arn:aws:eks:{region}:{account}:cluster/{cluster}"
    require(outputs["cluster_arn"] == cluster_arn)
    node = outputs["workspace_node_group"]
    prerequisites = outputs["tenant_scheduling_prerequisites"]
    observed = metadata["provider_snapshot"]
    require(
        all(
            observed[key] == value and isinstance(value, str) and value
            for key, value in {
                "cluster_arn": cluster_arn,
                "nodegroup_arn": node["arn"],
                "node_role_arn": outputs["node_role_arn"],
                "launch_template_id": node["launch_template_id"],
                "launch_template_version": node["launch_template_version"],
                "cni_role_arn": prerequisites["cni_role_arn"],
                "cni_addon_version": prerequisites["cni_addon_version"],
                "sts_endpoint_id": outputs["sts_endpoint_id"],
            }.items()
        )
    )
    require(
        isinstance(observed["retained_sts_rule_id"], str)
        and observed["retained_sts_rule_id"]
    )

    def resource(kind, identity):
        require(
            isinstance(identity, str)
            and re.fullmatch(
                {
                    "vpc": r"vpc-[0-9a-f]{8}(?:[0-9a-f]{9})?",
                    "subnet": r"subnet-[0-9a-f]{8}(?:[0-9a-f]{9})?",
                    "security-group": r"sg-[0-9a-f]{8}(?:[0-9a-f]{9})?",
                }[kind],
                identity,
            )
        )
        return f"arn:aws:ec2:{region}:{account}:{kind}/{identity}"

    owned = [cluster_arn]
    for key in ("workspace_api_security_group_id", "workspace_node_security_group_id"):
        owned.append(resource("security-group", outputs[key]))
    network = [resource("vpc", outputs["vpc_id"])]
    for key in ("private_subnet_ids", "public_subnet_ids"):
        require(isinstance(outputs[key], list))
        network.extend(resource("subnet", subnet) for subnet in outputs[key])
    require(outputs["network_ownership"] in ("adp-created", "supplied"))
    preserved = network if outputs["network_ownership"] == "supplied" else []
    if not preserved:
        owned.extend(network)
    require(len(owned + preserved) == len(set(owned + preserved)))
    return {
        "status": "OBSERVED",
        "org_id": scope["org_id"],
        "workspace_id": scope["workspace_id"],
        "request_id": scope["request_id"],
        "original_operation_id": original_operation,
        "apply_operation_id": row["source_operation_id"],
        "artifact_id": row["artifact_id"],
        "recorded_at": row["created_at"].isoformat(),
        "owned_resources": owned,
        "preserved_resources": preserved,
        "inventory_complete": False,
    }


async def run(scope):
    from app.adapters.harness_connection import build_harness_connections
    from app.config import settings

    connections = build_harness_connections(settings)
    require(connections is not None)
    try:
        await connections.open()
        await connections.ensure_ready()
        return await collect(connections.connect, scope)
    finally:
        await connections.aclose()


if __name__ == "__main__":
    result = {
        "status": "BLOCKED",
        "reason": "immutable applied ownership unavailable or mismatched",
    }
    with suppress(Exception):
        result = asyncio.run(run(json.loads(sys.argv[1])))
    print(json.dumps(result, allow_nan=False))
