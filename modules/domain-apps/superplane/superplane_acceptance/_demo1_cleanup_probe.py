"""Read immutable managed cleanup preparation; never acquire execution authority."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from uuid import UUID


def require(condition):
    if not condition:
        raise ValueError("cleanup artifact observation refused")


async def collect(connect, scope):
    from harness_jobs.identity import decode_payload, payload_digest

    from workspace_provisioning.artifacts import canonical, digest, read_artifact
    from workspace_provisioning.retirement_access_plan import access_identity
    from workspace_provisioning.retirement_managed_access import (
        ManagedRetirementAccessPlan,
        require_managed_control_source,
    )

    org, workspace = scope["org_id"], scope["workspace_id"]
    async with connect() as connection:
        transaction = connection.transaction(isolation="repeatable_read", readonly=True)
        async with transaction:

            @asynccontextmanager
            async def snapshot():
                yield connection

            current = await connection.fetchrow(
                "SELECT provisioning_operation_id FROM workspaces WHERE id=$1 AND org_id=$2",
                UUID(workspace),
                UUID(org),
            )
            require(
                current
                and str(current["provisioning_operation_id"])
                == scope["source_operation_id"]
            )
            candidates = await connection.fetch(
                "SELECT a.artifact_id FROM workspace_lifecycle_control_operations c "
                "JOIN workspace_lifecycle_artifacts a ON a.source_operation_id=c.operation_id "
                "AND a.org_id=c.org_id AND a.workspace_id=c.workspace_id "
                "WHERE c.org_id=$1 AND c.workspace_id=$2 AND c.request_id=$3 "
                "AND c.source_bootstrap_operation_id=$4 AND c.phase='prepare-retirement-access' LIMIT 2",
                org,
                workspace,
                scope["retirement_request_id"],
                scope["source_operation_id"],
            )
            require(len(candidates) == 1)
            row = await read_artifact(
                snapshot,
                artifact_id=candidates[0]["artifact_id"],
                org_id=org,
                workspace_id=workspace,
                require_fresh=False,
            )
            request = decode_payload(row["source_request_payload"])
            parameters = dict(request.parameters)
            metadata = json.loads(row["artifact_metadata_json"])
            plan = ManagedRetirementAccessPlan(**metadata["retirement_access_plan"])
            require(
                (plan.request_id, plan.allocation_id)
                == access_identity(
                    org,
                    workspace,
                    scope["original_allocation_id"],
                    scope["retirement_request_id"],
                )
                and (plan.org_id, plan.workspace_id) == (org, workspace)
                and plan.original_allocation_id == scope["original_allocation_id"]
                and plan.retirement_request_id == scope["retirement_request_id"]
                and request.idempotency_key
                == scope["preparation_request_id"]
                == plan.request_id
                and payload_digest(request) == scope["preparation_revision"]
                and row["request_revision"] == scope["preparation_plan_revision"]
                and parameters["retirement_source_operation_id"]
                == scope["source_operation_id"]
                and parameters.get("retirement_prepare_destroy") == "v1"
                and datetime.fromisoformat(scope["authorized_at"])
                <= row["created_at"]
                <= datetime.fromisoformat(scope["observed_at"])
            )

            async def operation(identity, phase):
                record = await connection.fetchrow(
                    "SELECT * FROM harness_operations WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
                    identity,
                    org,
                    workspace,
                )
                require(record is not None)
                original = decode_payload(record["request_payload"])
                require(
                    record["state"] == "succeeded"
                    and record["operation_id"] == identity
                    and (record["org_id"], record["workspace_id"]) == (org, workspace)
                    and record["plan_digest"] == payload_digest(original)
                    and original.idempotency_key == record["idempotency_key"]
                    and original.action == "provision"
                    and original.parameters.get(
                        "lifecycle_phase", "prepare-infrastructure"
                    )
                    == phase
                    and original.parameters["plan_revision"] == scope["plan_revision"]
                    and all(
                        original.parameters[key] == parameters[key]
                        for key in (
                            "lifecycle_request",
                            "lifecycle_inputs",
                            "aws_account_id",
                        )
                    )
                )
                return original

            bootstrap = await operation(
                scope["source_operation_id"], "bootstrap-workspace"
            )
            paid_id = bootstrap.parameters["lifecycle_source_operation_id"]
            paid = await operation(paid_id, "apply-infrastructure")
            root = await operation(
                paid.parameters["lifecycle_source_operation_id"],
                "prepare-infrastructure",
            )
            require(root.idempotency_key == scope["request_id"])
            control = await require_managed_control_source(
                connection,
                plan=plan,
                access_artifact=row,
                paid_operation_id=paid_id,
            )
            approved = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM harness_approval_consumption WHERE approval_id=$1 "
                "AND operation_id=$2 AND org_id=$3 AND workspace_id=$4 AND plan_digest=$5 "
                "AND reservation_state IN ('confirmed','retained'))",
                scope["approval_id"],
                control,
                org,
                workspace,
                scope["preparation_revision"],
            )
            destroy, fence = metadata["reviewed_destroy"], metadata["retirement_fence"]
            require(
                approved
                and row["account_id"] == scope["account"]
                and plan.cluster_arn.split(":")[3:5]
                == [scope["region"], scope["account"]]
                and all(
                    destroy["target"][key] == value
                    for key, value in {
                        "org_id": org,
                        "workspace_id": workspace,
                        "account_id": scope["account"],
                        "aws_region": scope["region"],
                    }.items()
                )
                and fence["identity"]
                == plan.fence_recipe["activate-retirement-fence"]["arguments"]
                and type(row["producer_fence_token"]) is int
                and row["producer_fence_token"] > 0
            )
            retirement = {
                key: value
                for key, value in parameters.items()
                if key
                not in {
                    "retirement_access_recipe_sha256",
                    "retirement_access_plan_sha256",
                    "retirement_prepare_destroy",
                    "plan_revision",
                    "execution_steps",
                }
            }
            retirement.update(
                lifecycle_phase="retire-workspace",
                allocation_id=plan.original_allocation_id,
                control_allocation_id=plan.allocation_id,
                cleanup_allocation_ids=canonical(
                    [plan.allocation_id, bootstrap.parameters["allocation_id"]]
                ),
                retirement_access_artifact_id=row["artifact_id"],
                terraform_plan_file_sha256=destroy["plan_file_sha256"],
                terraform_backend_sha256=destroy["backend_sha256"],
                managed_workload_inventory_sha256=fence[
                    "managed_workload_inventory_sha256"
                ],
            )
            return {
                "status": "OBSERVED",
                "scope": scope,
                "artifact_id": row["artifact_id"],
                "operation_id": control,
                "recorded_at": row["created_at"].isoformat(),
                "producer_attempt_id": row["producer_attempt_id"],
                "producer_fence_token": row["producer_fence_token"],
                "grant_count": len(metadata["grants"]),
                "grant_set_sha256": digest(metadata["grants"]),
                "fence_sha256": digest(fence),
                "inventory_sha256": plan.inventory_sha256,
                "managed_workload_inventory_sha256": fence[
                    "managed_workload_inventory_sha256"
                ],
                "destroy_sha256": digest(destroy),
                "plan_file_sha256": destroy["plan_file_sha256"],
                "plan_json_sha256": destroy["plan_json_sha256"],
                "backend_sha256": destroy["backend_sha256"],
                "retirement_plan_sha256": digest(retirement),
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
        "reason": "immutable cleanup preparation unavailable or mismatched",
    }
    with suppress(Exception):
        result = asyncio.run(run(json.loads(sys.argv[1])))
    print(json.dumps(result, allow_nan=False))
