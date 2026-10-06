"""Fixed read-only probe for admitted dedicated-workspace continuation ancestry."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from uuid import UUID


def require(condition):
    if not condition:
        raise ValueError("continuation lineage refused")


async def collect(connect, scope):
    from harness_jobs.identity import decode_payload, payload_digest

    from workspace_provisioning.artifacts import (
        continuation_parameters,
        initial_execution_steps,
        read_artifact,
    )

    org, workspace = scope["org_id"], scope["workspace_id"]
    async with connect() as connection:
        transaction = connection.transaction(isolation="repeatable_read", readonly=True)
        async with transaction:

            @asynccontextmanager
            async def snapshot():
                yield connection

            current_workspace = await connection.fetchrow(
                "SELECT provisioning_operation_id FROM workspaces WHERE id=$1 AND org_id=$2",
                UUID(workspace),
                UUID(org),
            )
            require(
                current_workspace is not None
                and str(current_workspace["provisioning_operation_id"])
                == scope["current_operation_id"]
            )
            roots = await connection.fetch(
                "SELECT * FROM harness_operations WHERE org_id=$1 AND workspace_id=$2 "
                "AND idempotency_key=$3 LIMIT 2",
                org,
                workspace,
                scope["request_id"],
            )
            require(len(roots) == 1)
            root = roots[0]
            require(root["operation_id"] == scope["original_operation_id"])

            async def operation(identity):
                observed = await connection.fetchrow(
                    "SELECT * FROM harness_operations WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
                    identity,
                    org,
                    workspace,
                )
                require(observed is not None)
                request = decode_payload(observed["request_payload"])
                require(
                    observed["org_id"] == org
                    and observed["workspace_id"] == workspace
                    and observed["operation_id"] == identity
                    and observed["plan_digest"] == payload_digest(request)
                    and observed["idempotency_key"] == request.idempotency_key
                    and request.action == "provision"
                    and request.parameters["plan_revision"] == scope["plan_revision"]
                    and json.loads(request.parameters["lifecycle_request"])["mode"]
                    == "existing-account-managed"
                )
                return observed, dict(request.parameters)

            current, parameters = await operation(scope["current_operation_id"])
            current_request = current["idempotency_key"]
            current_phase = parameters.get("lifecycle_phase")
            require(current_phase in ("apply-infrastructure", "bootstrap-workspace"))

            def observed_operation(row, phase):
                return {
                    "phase": phase,
                    "request_id": row["idempotency_key"],
                    "operation_id": row["operation_id"],
                    "state": row["state"],
                }

            operations = [observed_operation(current, current_phase)]
            phases = (
                ("bootstrap-workspace", "apply-infrastructure")
                if current_phase == "bootstrap-workspace"
                else ("apply-infrastructure",)
            )
            artifacts, requests = [], {current_request}
            latest = datetime.fromisoformat(scope["observed_at"])
            for phase in phases:
                require(parameters.get("lifecycle_phase") == phase)
                parent = await read_artifact(
                    snapshot,
                    artifact_id=parameters["lifecycle_artifact_id"],
                    org_id=org,
                    workspace_id=workspace,
                    require_fresh=False,
                )
                require(
                    parameters == continuation_parameters(parent)
                    and parent["org_id"] == org
                    and parent["workspace_id"] == workspace
                    and parent["account_id"] == scope["account"]
                    and datetime.fromisoformat(scope["authorized_at"])
                    <= parent["created_at"]
                    <= latest
                    and all(
                        json.loads(parent["target_json"])[key] == value
                        for key, value in {
                            "org_id": org,
                            "workspace_id": workspace,
                            "account_id": scope["account"],
                            "aws_region": scope["region"],
                        }.items()
                    )
                )
                current, parameters = await operation(parent["source_operation_id"])
                require(
                    current["state"] == "succeeded"
                    and current["request_payload"] == parent["source_request_payload"]
                    and current["plan_digest"] == parent["source_payload_digest"]
                    and current["job_id"] == parent["source_job_id"]
                    and current["attempt_id"] == parent["source_attempt_id"]
                    and current["idempotency_key"] not in requests
                )
                requests.add(current["idempotency_key"])
                artifacts.append(parent["artifact_id"])
                operations.append(
                    observed_operation(
                        current,
                        parameters.get("lifecycle_phase", "prepare-infrastructure"),
                    )
                )
                latest = parent["created_at"]
            require(
                current == root
                and parameters.get("lifecycle_phase", "prepare-infrastructure")
                == "prepare-infrastructure"
                and parameters["execution_steps"] == initial_execution_steps(parameters)
            )
            return {
                "status": "OBSERVED",
                **scope,
                "current_request_id": current_request,
                "current_phase": current_phase,
                "artifact_ids": artifacts,
                "operations": list(reversed(operations)),
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
        "reason": "immutable continuation lineage unavailable",
    }
    with suppress(Exception):
        result = asyncio.run(run(json.loads(sys.argv[1])))
    print(json.dumps(result, allow_nan=False))
