"""Read immutable managed cleanup preparation; never acquire execution authority."""

import asyncio
import json
import re
import sys
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import asdict, replace
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID


def require(condition):
    if not condition:
        raise ValueError("cleanup artifact observation refused")


async def canonical_inventory(connection, org, workspace):
    """Observe canonical records in one snapshot, never acquire mutation authority."""
    from superplane_bootstrap.registry import _LOCK, _LOCK_PREFIX, SqlRegistrationStore

    from workspace_provisioning.process import AsyncBridgeStore
    from workspace_provisioning.retirement_inventory import (
        load_bootstrap_retirement_review,
    )

    require(
        connection.is_in_transaction()
        and await connection.fetchval("SHOW transaction_read_only") == "on"
        and await connection.fetchval("SHOW transaction_isolation") == "repeatable read"
    )

    class SnapshotStore(AsyncBridgeStore):
        @contextmanager
        def transaction(self):
            yield

        def execute(self, statement, parameters):
            statement = statement.strip()
            if statement == _LOCK:
                require(parameters == {"binding": _LOCK_PREFIX + workspace})
                return []
            require(
                statement.startswith("SELECT ")
                and ";" not in statement
                and parameters.get("workspace_id") == workspace
                and parameters.get("org_id", org) == org
                and parameters.get("org", org) == org
            )
            statement = re.sub(r" FOR UPDATE(?: OF [a-z, ]+)?$", "", statement)
            require("FOR UPDATE" not in statement)
            return super().execute(statement, parameters)

    store = SnapshotStore(None, asyncio.get_running_loop())
    store.connection = connection
    return await asyncio.to_thread(
        load_bootstrap_retirement_review,
        registration_store=SqlRegistrationStore(store),
        workspace_id=workspace,
        org_id=org,
    )


async def collect(connect, scope, policy):
    from harness_jobs.identity import decode_payload, payload_digest

    from workspace_provisioning.artifacts import canonical, digest, read_artifact
    from workspace_provisioning.retirement_access_plan import access_identity
    from workspace_provisioning.retirement_managed_access import (
        ManagedRetirementAccessPlan,
        require_managed_control_source,
    )
    from workspace_provisioning.retirement_request import retirement_request

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
            sequences = (
                "registrar_namespaces",
                "owned_objects",
                "revocation_order",
                "grants",
                "retained_grants",
            )
            require(
                all(isinstance(getattr(plan, key), (list, tuple)) for key in sequences)
            )
            plan = replace(
                plan, **{key: tuple(getattr(plan, key)) for key in sequences}
            )
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

            operations = {}

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
                operations[identity] = dict(record)
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
            inventory = await canonical_inventory(connection, org, workspace)
            source = SimpleNamespace(
                **operations[scope["source_operation_id"]],
                admitted_request=lambda: bootstrap,
            )
            deletion_request, deletion = retirement_request(
                inventory, plan, row, source, policy
            )
            require(
                deletion.completes_teardown
                and deletion_request.parameters["plan_revision"] == digest(retirement)
            )
            transport = await cluster_transport(
                snapshot, plan, bootstrap, operations[paid_id]
            )
            kubernetes = {
                "transport": transport,
                "grants": [
                    asdict(grant)
                    for grant in inventory.grants
                    if grant.spec["key"].startswith("cleanup-")
                    or grant.spec["key"]
                    in {"retirement-fence-policy", "retirement-fence-binding"}
                ],
                "fence": fence,
            }
            return {
                "status": "OBSERVED",
                "scope": scope,
                "artifact_id": row["artifact_id"],
                "operation_id": control,
                "recorded_at": row["created_at"].isoformat(),
                "producer_attempt_id": row["producer_attempt_id"],
                "producer_fence_token": row["producer_fence_token"],
                "grant_count": len(metadata["grants"]),
                "grants": metadata["grants"],
                "kubernetes": kubernetes,
                "kubernetes_inventory_sha256": digest(kubernetes),
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
                "retirement_revision_sha256": payload_digest(deletion_request),
            }


async def cluster_transport(connect, plan, bootstrap, paid):
    from account_factory.modes import from_mapping

    from workspace_provisioning.artifacts import read_artifact
    from workspace_provisioning.retirement_managed_access import (
        verify_managed_access_artifact,
    )

    require(plan.bootstrap_artifact_id == bootstrap.parameters["lifecycle_artifact_id"])
    row = await read_artifact(
        connect,
        artifact_id=plan.bootstrap_artifact_id,
        org_id=plan.org_id,
        workspace_id=plan.workspace_id,
        require_fresh=False,
    )
    require(
        bootstrap.parameters["lifecycle_source_operation_id"] == paid["operation_id"]
        and all(
            row[artifact] == paid[source]
            for artifact, source in (
                ("source_operation_id", "operation_id"),
                ("source_job_id", "job_id"),
                ("source_attempt_id", "attempt_id"),
                ("source_payload_digest", "plan_digest"),
                ("source_request_payload", "request_payload"),
            )
        )
    )
    outputs = verify_managed_access_artifact(
        row,
        from_mapping(json.loads(bootstrap.parameters["lifecycle_request"])),
        plan,
    )
    return {
        key: outputs[key]
        for key in (
            "cluster_arn",
            "cluster_name",
            "cluster_endpoint",
            "cluster_certificate_authority_data",
        )
    }


async def run(scope):
    from app.adapters.harness_connection import build_harness_connections
    from app.config import settings
    from app.services.onboarding import policy_for

    connections = build_harness_connections(settings)
    require(connections is not None)
    try:
        await connections.open()
        await connections.ensure_ready()
        return await collect(connections.connect, scope, policy_for(scope["org_id"]))
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
