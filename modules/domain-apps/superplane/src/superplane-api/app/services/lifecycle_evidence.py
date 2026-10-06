"""Read-only projections over independently authorized operation/domain stores."""

import json
import re
from datetime import UTC, datetime

from harness_jobs.admission import derive_operation_identity
from harness_jobs.identity import decode_payload, payload_digest
from sqlalchemy import select

from app.models.workspace import Workspace
from app.schemas.lifecycle_evidence import (
    AppliedOwnershipEvidence,
    CleanupPreparationEvidence,
)
from workspace_provisioning.artifacts import digest, read_artifact
from workspace_provisioning.runtime_config import LifecycleRefused


def require(condition):
    if not condition:
        raise LifecycleRefused("historical lifecycle evidence differs")


async def workspace_pointer_matches(db, org_id, workspace_id, request_id, operation_id):
    # Scalar columns avoid the ORM identity map returning the pre-observation row.
    row = (
        await db.execute(
            select(Workspace.operation_id, Workspace.provisioning_operation_id).where(
                Workspace.org_id == org_id, Workspace.id == workspace_id
            )
        )
    ).one_or_none()
    return row is not None and (str(row[0]), row[1]) == (str(request_id), operation_id)


async def native_evidence(composition, **scope):
    from workspace_provisioning.lineage import verified_native_lineage

    lineage = await verified_native_lineage(
        composition.operation_connect, composition.domain_connect, **scope
    )
    ownership = None
    try:
        if len(lineage["phases"]) == 3:
            ownership = await _applied_ownership(composition.domain_connect, lineage)
    except Exception:
        # Existing recovery/history remains useful; absent proof is never success.
        pass
    return lineage, ownership


async def _applied_ownership(domain_connect, lineage):
    """The lineage verifier has checked every original paid approval/admission.

    Select its exact applied artifact, never an unbound workspace-wide candidate.
    Its digest was already part of that chain and is verified again on this read.
    """
    apply, bootstrap = lineage["phases"][1:]
    row = await read_artifact(
        domain_connect,
        artifact_id=bootstrap["source_artifact_id"],
        org_id=lineage["org_id"],
        workspace_id=lineage["workspace_id"],
        require_fresh=False,
    )
    metadata = json.loads(row["artifact_metadata_json"])
    parameters = json.loads(row["parameters_json"])
    target = json.loads(row["target_json"])
    require(
        metadata["next_phase"] == "bootstrap-workspace"
        and apply["phase"] == "apply-infrastructure"
        and apply["state"] == "succeeded"
        and row["source_operation_id"] == apply["operation_id"]
        and row["source_payload_digest"] == apply["payload_digest"]
        and row["request_revision"] == lineage["plan_revision"]
        and metadata["allocation_source_operation_id"] == apply["operation_id"]
        and metadata["source_artifact_id"] == apply["source_artifact_id"]
        and parameters["lifecycle_artifact_id"] == apply["source_artifact_id"]
        and row["created_at"] <= datetime.now(UTC)
    )
    account, region = row["account_id"], target["aws_region"]
    outputs = metadata["outputs"]

    # Only explicit identity outputs leave the service. Other outputs may include
    # credential references or sensitive values and are never copied wholesale.
    def output(name):
        value = outputs[name]
        require(isinstance(value, dict) and value.get("sensitive") is False)
        return value["value"]

    for key, expected in {
        "org_id": lineage["org_id"],
        "workspace_id": lineage["workspace_id"],
        "account_id": account,
        "aws_region": region,
    }.items():
        require(target[key] == expected and output(key) == expected)
    cluster = output("cluster_name")
    require(
        isinstance(cluster, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", cluster)
    )
    cluster_arn = f"arn:aws:eks:{region}:{account}:cluster/{cluster}"
    require(output("cluster_arn") == cluster_arn)
    node, prerequisites = (
        output("workspace_node_group"),
        output("tenant_scheduling_prerequisites"),
    )
    observed = metadata["provider_snapshot"]
    for key, value in {
        "cluster_arn": cluster_arn,
        "nodegroup_arn": node["arn"],
        "node_role_arn": output("node_role_arn"),
        "launch_template_id": node["launch_template_id"],
        "launch_template_version": node["launch_template_version"],
        "cni_role_arn": prerequisites["cni_role_arn"],
        "cni_addon_version": prerequisites["cni_addon_version"],
        "sts_endpoint_id": output("sts_endpoint_id"),
    }.items():
        require(isinstance(value, str) and value and observed[key] == value)
    require(
        isinstance(observed["retained_sts_rule_id"], str)
        and observed["retained_sts_rule_id"]
    )

    def resource(kind, identity):
        prefix = {"vpc": "vpc", "subnet": "subnet", "security-group": "sg"}[kind]
        require(
            isinstance(identity, str)
            and re.fullmatch(prefix + r"-[0-9a-f]{8}(?:[0-9a-f]{9})?", identity)
        )
        return f"arn:aws:ec2:{region}:{account}:{kind}/{identity}"

    owned = [cluster_arn] + [
        resource("security-group", output(key))
        for key in (
            "workspace_api_security_group_id",
            "workspace_node_security_group_id",
        )
    ]
    network = [resource("vpc", output("vpc_id"))]
    for key in ("private_subnet_ids", "public_subnet_ids"):
        subnets = output(key)
        require(isinstance(subnets, list) and len(subnets) <= 64)
        network.extend(resource("subnet", subnet) for subnet in subnets)
    require(output("network_ownership") in {"adp-created", "supplied"})
    preserved = network if output("network_ownership") == "supplied" else []
    if not preserved:
        owned.extend(network)
    require(len(owned + preserved) == len(set(owned + preserved)) <= 128)
    return AppliedOwnershipEvidence(
        org_id=lineage["org_id"],
        workspace_id=lineage["workspace_id"],
        request_id=lineage["root_request_id"],
        original_operation_id=lineage["root_operation_id"],
        current_operation_id=lineage["current_operation_id"],
        apply_operation_id=apply["operation_id"],
        plan_revision=lineage["plan_revision"],
        account_id=account,
        region=region,
        artifact_id=row["artifact_id"],
        recorded_at=row["created_at"],
        owned_resources=owned,
        preserved_resources=preserved,
    ).model_dump(mode="json")


async def cleanup_preparation(
    operation_connect, *, row, plan, request, source_operation_id
):
    """Project the already-verified control artifact and its concrete paid receipt."""
    original = decode_payload(row["source_request_payload"])
    async with operation_connect() as connection:
        paid = await connection.fetchrow(
            "SELECT * FROM harness_approval_consumption WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
            row["source_operation_id"],
            plan.org_id,
            plan.workspace_id,
        )
    require(
        paid is not None
        and paid["plan_digest"]
        == row["source_payload_digest"]
        == payload_digest(original)
        and paid["reservation_state"] in {"confirmed", "retained"}
        and paid["reservation_id"]
        and paid["requester"]
        and paid["approved_by"]
        and paid["requester"] != paid["approved_by"]
        and derive_operation_identity(paid["approval_id"])
        == (row["source_operation_id"], row["source_job_id"], row["source_attempt_id"])
        and original.idempotency_key == plan.request_id
        and row["created_at"] <= datetime.now(UTC)
        and request.parameters["retirement_access_artifact_id"] == row["artifact_id"]
        and request.parameters["retirement_source_operation_id"] == source_operation_id
        and original.parameters["retirement_source_operation_id"] == source_operation_id
    )
    metadata = json.loads(row["artifact_metadata_json"])
    destroy, fence = metadata["reviewed_destroy"], metadata["retirement_fence"]
    return CleanupPreparationEvidence(
        org_id=plan.org_id,
        workspace_id=plan.workspace_id,
        source_operation_id=source_operation_id,
        retirement_request_id=plan.retirement_request_id,
        preparation_request_id=plan.request_id,
        preparation_revision=payload_digest(original),
        preparation_plan_revision=row["request_revision"],
        preparation_approval_id=paid["approval_id"],
        artifact_id=row["artifact_id"],
        operation_id=row["source_operation_id"],
        recorded_at=row["created_at"],
        producer_attempt_id=row["producer_attempt_id"],
        producer_fence_token=row["producer_fence_token"],
        grant_count=len(metadata["grants"]),
        grant_set_sha256=digest(metadata["grants"]),
        fence_sha256=digest(fence),
        inventory_sha256=plan.inventory_sha256,
        managed_workload_inventory_sha256=fence["managed_workload_inventory_sha256"],
        destroy_sha256=digest(destroy),
        plan_file_sha256=destroy["plan_file_sha256"],
        plan_json_sha256=destroy["plan_json_sha256"],
        backend_sha256=destroy["backend_sha256"],
        retirement_plan_sha256=request.parameters["plan_revision"],
    )
