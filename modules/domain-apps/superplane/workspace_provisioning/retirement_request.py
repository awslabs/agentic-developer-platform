"""Compile the separately approved removal from immutable paid access evidence."""

import json
from dataclasses import asdict

from harness_jobs.identity import OperationRequest, payload_digest

from .artifacts import digest
from .lifecycle_policy import policy_digest, policy_document
from .retirement_access_artifact import validate_access_artifact
from .retirement_destroy_producer import destroy_reference
from .retirement_plan import compose_retirement_plan
from .runtime_config import LifecycleRefused

PHASE = "retire-workspace"


def retirement_request(inventory, access_plan, access_row, source, policy):
    policy = policy_document(policy)
    validate_access_artifact(access_row, access_plan)
    metadata = json.loads(access_row["artifact_metadata_json"])
    reference = destroy_reference(metadata["reviewed_destroy"])
    original = source.admitted_request()
    previous = original.parameters
    account, region = (
        inventory.cluster_arn.split(":")[4],
        inventory.cluster_arn.split(":")[3],
    )
    if (
        inventory.preserve_cluster
        or inventory.cluster_ownership != "adp-created"
        or json.loads(previous["lifecycle_request"]).get("mode")
        != "existing-account-managed"
        or json.loads(previous["lifecycle_inputs"]).get("isolation_mode") != "dedicated"
        or (source.org_id, source.workspace_id)
        != (inventory.org_id, inventory.workspace_id)
        or source.state != "succeeded"
        or payload_digest(original) != source.plan_digest
        or source.plan_digest
        != json.loads(access_row["parameters_json"])["retirement_source_payload_digest"]
        or reference.original_allocation_id != access_plan.original_allocation_id
        or any(
            reference.target.get(key) != value
            for key, value in {
                "org_id": inventory.org_id,
                "workspace_id": inventory.workspace_id,
                "account_id": account,
                "aws_region": region,
            }.items()
        )
        or metadata["retirement_fence"]["identity"]
        != (access_plan.fence_recipe or {})
        .get("activate-retirement-fence", {})
        .get("arguments")
        or access_plan.runtime_config_sha256 != digest(policy["runtime"])
        or "managed" not in policy["permitted_modes"]
        or account not in policy["permitted_target_accounts"]
        or region not in policy["permitted_regions"]
        or not inventory.components_complete
    ):
        raise LifecycleRefused("retirement differs from its original managed ownership")
    plan = compose_retirement_plan(
        inventory, managed_destroy=reference, managed_access=(access_plan, access_row)
    )
    parameters = {
        "lifecycle_phase": PHASE,
        "lifecycle_request": previous["lifecycle_request"],
        "lifecycle_inputs": previous["lifecycle_inputs"],
        "lifecycle_artifact_id": previous["lifecycle_artifact_id"],
        "retirement_request_id": access_plan.retirement_request_id,
        "retirement_source_operation_id": source.operation_id,
        "retirement_source_job_id": source.job_id,
        "retirement_source_attempt_id": source.attempt_id,
        "retirement_source_payload_digest": source.plan_digest,
        "retirement_inventory_sha256": digest(asdict(inventory)),
        "retirement_access_artifact_id": access_row["artifact_id"],
        "control_allocation_id": access_plan.allocation_id,
        "allocation_id": access_plan.original_allocation_id,
        "original_allocation_id": access_plan.original_allocation_id,
        "terraform_plan_file_sha256": reference.plan_file_sha256,
        "terraform_backend_sha256": reference.backend_sha256,
        "managed_workload_inventory_sha256": metadata["retirement_fence"][
            "managed_workload_inventory_sha256"
        ],
        "lifecycle_policy_sha256": policy_digest(policy),
        "runtime_config_sha256": digest(policy["runtime"]),
        "aws_account_id": account,
        "provider_account_id": account,
        "provider": "aws",
        "region": region,
        "max_resource_units": "0",
        "max_cost_micros": "0",
        "max_runtime_seconds": str(policy["operation_max_runtime_seconds"]),
        **policy["credential_references"][account],
    }
    parameters["plan_revision"] = digest(parameters)
    parameters["execution_steps"] = plan.encode()
    return OperationRequest(
        action="teardown",
        idempotency_key=access_plan.retirement_request_id,
        parameters=parameters,
    ), plan
