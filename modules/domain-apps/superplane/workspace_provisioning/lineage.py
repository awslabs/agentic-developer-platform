"""Bounded historical native lineage; proof of identity, never proof of readiness.

Call only after authenticating the original operation's workspace scope. Expired
artifacts remain history; reading them here grants no admission or effect authority.
"""

import json
import re

from harness_jobs.admission import derive_operation_identity
from harness_jobs.identity import decode_payload, payload_digest

from .artifacts import continuation_parameters, initial_execution_steps, read_artifact
from .runtime_config import LifecycleRefused

PHASES = ("prepare-infrastructure", "apply-infrastructure", "bootstrap-workspace")


async def verified_native_lineage(
    operation_connect,
    domain_connect,
    *,
    org_id,
    workspace_id,
    root_operation_id,
    current_operation_id,
):
    """Read at most three paid operations and two immutable continuation artifacts."""
    async with operation_connect() as connection:
        operations = {}

        async def operation(operation_id):
            if operation_id in operations:
                return operations[operation_id]
            if len(operations) >= len(PHASES):
                raise LifecycleRefused("lifecycle lineage exceeds its operation bound")
            row = await connection.fetchrow(
                "SELECT * FROM harness_operations WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
                operation_id,
                org_id,
                workspace_id,
            )
            if row is None:
                raise LifecycleRefused("lifecycle operation is unavailable")
            admitted = decode_payload(row["request_payload"])
            if (
                payload_digest(admitted) != row["plan_digest"]
                or admitted.action != "provision"
                or admitted.idempotency_key != row["idempotency_key"]
            ):
                raise LifecycleRefused("lifecycle admission identity differs")
            paid = await connection.fetchrow(
                "SELECT * FROM harness_approval_consumption WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
                operation_id,
                org_id,
                workspace_id,
            )
            if (
                paid is None
                or paid["plan_digest"] != row["plan_digest"]
                or not paid["reservation_id"]
                or paid["reservation_state"]
                not in {"confirmed", "retained", "released"}
                or not paid["requester"]
                or not paid["approved_by"]
                or paid["requester"] == paid["approved_by"]
                or derive_operation_identity(paid["approval_id"])
                != (row["operation_id"], row["job_id"], row["attempt_id"])
            ):
                raise LifecycleRefused("lifecycle paid admission differs")
            operations[operation_id] = (dict(row), admitted)
            return operations[operation_id]

        root, original = await operation(root_operation_id)
        parameters = original.parameters
        request = json.loads(parameters["lifecycle_request"])
        public = json.loads(parameters["lifecycle_inputs"])
        if (
            request.get("mode") != "existing-account-managed"
            or request.get("workspace_id") != workspace_id
            or public.get("cluster_placement", "dedicated") != "dedicated"
            or public.get("shared_cluster_id") is not None
            or parameters.get("lifecycle_phase", PHASES[0]) != PHASES[0]
            or "lifecycle_source_operation_id" in parameters
            or "lifecycle_artifact_id" in parameters
            or parameters.get("execution_steps") != initial_execution_steps(parameters)
            or not re.fullmatch(r"[a-f0-9]{64}", parameters.get("plan_revision", ""))
        ):
            raise LifecycleRefused("original dedicated managed phase differs")
        chain, seen = [], set()
        next_id = current_operation_id
        for _ in PHASES:
            if next_id in seen:
                raise LifecycleRefused("lifecycle lineage is cyclic")
            seen.add(next_id)
            row, admitted = (
                (root, original)
                if next_id == root_operation_id
                else await operation(next_id)
            )
            phase = admitted.parameters.get("lifecycle_phase", PHASES[0])
            entry = {
                "operation_id": row["operation_id"],
                "request_id": row["idempotency_key"],
                "payload_digest": row["plan_digest"],
                "phase": phase,
                "state": row["state"],
                "source_artifact_id": admitted.parameters.get("lifecycle_artifact_id"),
            }
            chain.append(entry)
            if next_id == root_operation_id:
                break
            artifact = await read_artifact(
                domain_connect,
                artifact_id=admitted.parameters.get("lifecycle_artifact_id"),
                org_id=org_id,
                workspace_id=workspace_id,
                require_fresh=False,
            )
            if dict(admitted.parameters) != continuation_parameters(artifact):
                raise LifecycleRefused(
                    "lifecycle continuation differs from reviewed artifact"
                )
            source, source_request = await operation(artifact["source_operation_id"])
            if (
                source["state"] != "succeeded"
                or source["request_payload"] != artifact["source_request_payload"]
                or source["plan_digest"] != artifact["source_payload_digest"]
                or source["job_id"] != artifact["source_job_id"]
                or source["attempt_id"] != artifact["source_attempt_id"]
                or dict(source_request.parameters)
                != json.loads(artifact["parameters_json"])
            ):
                raise LifecycleRefused("lifecycle producer original admission differs")
            next_id = source["operation_id"]
        chain.reverse()
        if (
            chain[0]["operation_id"] != root_operation_id
            or tuple(item["phase"] for item in chain) != PHASES[: len(chain)]
        ):
            raise LifecycleRefused(
                "lifecycle phases are not a contiguous original chain"
            )
        return {
            "version": 1,
            "org_id": org_id,
            "workspace_id": workspace_id,
            "root_request_id": root["idempotency_key"],
            "root_operation_id": root_operation_id,
            "current_operation_id": current_operation_id,
            "plan_revision": parameters["plan_revision"],
            "phases": chain,
        }
