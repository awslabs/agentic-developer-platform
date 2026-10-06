"""Compile both approved phases from the actual bootstrap ownership journal.

Provider results are explicit fixture facts. No preview/compiler is mocked and
this test does not claim a real cloud deletion.
"""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from harness_jobs.identity import OperationRequest, encode_payload, payload_digest

from workspace_provisioning.artifacts import canonical, digest
from workspace_provisioning.retirement_access_artifact import (
    access_metadata,
    access_target,
)
from workspace_provisioning.retirement_access_authority import access_request
from workspace_provisioning.retirement_destroy_producer import FILES
from workspace_provisioning.retirement_managed_access import (
    compile_managed_access_review,
)
from workspace_provisioning.retirement_request import retirement_request
from workspace_provisioning.runtime_config import LifecycleRefused

from .test_lifecycle_policy import policy
from .test_retirement_access_artifact import grant_identities
from .test_retirement_managed_access import inputs
from .test_retirement_plan import component


@pytest.fixture
def composed(runtime):
    arguments = inputs(runtime)
    inventory = replace(
        arguments["inventory"],
        components_complete=True,
        components=(
            component("fixture-controller", namespace=arguments["inventory"].namespace),
        ),
    )
    plan = compile_managed_access_review(
        inventory,
        arguments["runtime"],
        prepare_destroy=True,
        **{
            key: value
            for key, value in arguments.items()
            if key not in {"inventory", "runtime", "kubernetes", "eks"}
        },
    )
    account, region = plan.cluster_arn.split(":")[4], plan.cluster_arn.split(":")[3]
    deployment = policy()
    deployment["runtime"] = arguments["runtime"]
    deployment["permitted_target_accounts"] = [account]
    deployment["permitted_regions"] = [region]
    deployment["credential_references"] = {
        account: next(iter(deployment["credential_references"].values()))
    }
    original = OperationRequest(
        "provision",
        "bootstrap-request",
        {
            "allocation_id": "bootstrap-allocation",
            "lifecycle_phase": "bootstrap-workspace",
            "lifecycle_source_operation_id": "completed-apply",
            "lifecycle_artifact_id": "a" * 64,
            "lifecycle_request": canonical(
                {
                    "mode": "existing-account-managed",
                    "region": region,
                    "target_account_id": account,
                    "workspace_id": plan.workspace_id,
                }
            ),
            "lifecycle_inputs": canonical({"isolation_mode": "dedicated"}),
            "aws_account_id": account,
        },
    )
    source = SimpleNamespace(
        state="succeeded",
        operation_id="completed-bootstrap",
        org_id=plan.org_id,
        workspace_id=plan.workspace_id,
        job_id="bootstrap-job",
        attempt_id="bootstrap-attempt",
        plan_digest=payload_digest(original),
        request_payload=encode_payload(original),
        admitted_request=lambda: original,
    )
    paid = SimpleNamespace(
        state="succeeded",
        operation_id="completed-apply",
        org_id=plan.org_id,
        workspace_id=plan.workspace_id,
        admitted_request=lambda: OperationRequest(
            "provision",
            "apply-request",
            {
                "allocation_id": "original-allocation",
                "lifecycle_phase": "apply-infrastructure",
            },
        ),
    )
    preparation = access_request(
        plan, source, deployment, allocation_source=paid, prepare_destroy=True
    )
    destroy = {
        "version": 1,
        "target": {
            "account_id": account,
            "aws_region": region,
            "org_id": plan.org_id,
            "workspace_id": plan.workspace_id,
            "environment": "dev",
            "workspace_name": "fixture",
        },
        "files": {name: "d" * 64 for name in FILES},
        "module_sha256": "e" * 64,
        "plan_file_sha256": "d" * 64,
        "plan_json_sha256": "d" * 64,
        "backend_sha256": "f" * 64,
        "original_allocation_id": plan.original_allocation_id,
    }
    fence = {
        "version": 1,
        "identity": plan.fence_recipe["activate-retirement-fence"]["arguments"],
        "managed_workload_inventory": [],
        "managed_workload_inventory_sha256": digest([]),
    }
    access = {
        "artifact_id": "c" * 64,
        "source_operation_id": "prepared-control",
        "org_id": plan.org_id,
        "workspace_id": plan.workspace_id,
        "account_id": account,
        "parameters_json": canonical(dict(preparation.parameters)),
        "target_json": canonical(access_target(plan)),
        "artifact_metadata_json": canonical(
            access_metadata(
                plan,
                grant_identities(plan),
                reviewed_destroy=destroy,
                retirement_fence=fence,
            )
        ),
    }
    return inventory, plan, access, source, deployment, preparation


def test_original_bootstrap_compiles_separate_preparation_and_teardown(composed):
    inventory, plan, access, source, deployment, preparation = composed
    removal, deletion = retirement_request(inventory, plan, access, source, deployment)
    assert preparation.action == "provision"
    assert preparation.parameters["retirement_prepare_destroy"] == "v1"
    assert removal.action == "teardown"
    assert removal.idempotency_key != preparation.idempotency_key
    assert payload_digest(removal) != payload_digest(preparation)
    assert removal.parameters["allocation_id"] == plan.original_allocation_id
    assert (
        removal.parameters["control_allocation_id"]
        == preparation.parameters["allocation_id"]
    )
    assert removal.parameters["retirement_access_artifact_id"] == access["artifact_id"]
    assert removal.parameters["execution_steps"] == deletion.encode()
    assert deletion.completes_teardown
    assert any(
        step.step_id == "destroy-managed-infrastructure" for step in deletion.steps
    )
    assert retirement_request(inventory, plan, access, source, deployment) == (
        removal,
        deletion,
    )


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "policy",
        "allocation",
        "fence",
        "missing_destroy",
        "adopted",
        "fence_identity",
        "destroy_target",
    ],
)
def test_changed_preparation_cannot_compile_removal(composed, change):
    inventory, plan, access, source, deployment, _ = composed
    if change == "source":
        source.plan_digest = "0" * 64
    elif change == "policy":
        deployment["runtime"]["environment"] = "prod"
    elif change == "adopted":
        inventory = replace(inventory, cluster_ownership="adopted")
    else:
        metadata = json.loads(access["artifact_metadata_json"])
        if change == "allocation":
            metadata["reviewed_destroy"]["original_allocation_id"] = (
                "foreign-allocation"
            )
        elif change == "fence_identity":
            metadata["retirement_fence"]["identity"]["policy_uid"] = "replacement-uid"
        elif change == "destroy_target":
            metadata["reviewed_destroy"]["target"]["workspace_id"] = "foreign-workspace"
        elif change == "fence":
            metadata["retirement_fence"]["managed_workload_inventory_sha256"] = "0" * 64
        else:
            metadata.pop("reviewed_destroy")
        access["artifact_metadata_json"] = canonical(metadata)
    with pytest.raises(LifecycleRefused):
        retirement_request(inventory, plan, access, source, deployment)
