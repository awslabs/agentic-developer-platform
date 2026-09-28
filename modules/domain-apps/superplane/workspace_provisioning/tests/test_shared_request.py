"""Shared placement cannot be laundered through dedicated continuation bytes."""

import json
from uuid import uuid4

import pytest
from superplane_bootstrap.membership import SharedMembership
from workspace_provisioning.shared_membership import approved_membership
from workspace_provisioning.runtime_config import LifecycleRefused


def request():
    binding = SharedMembership.create(
        org_id=str(uuid4()),
        workspace_id=str(uuid4()),
        cluster_id=str(uuid4()),
        request_id=str(uuid4()),
        cluster_arn="arn:aws:eks:us-east-1:123456789012:cluster/shared",
        endpoint="https://shared.example",
    )
    return binding, {
        "lifecycle_inputs": json.dumps(
            {
                "cluster_placement": "shared",
                "shared_cluster_id": binding.cluster_id,
            }
        ),
        "lifecycle_request": json.dumps(
            {
                "mode": "bring-existing-cluster",
                "workspace_id": binding.workspace_id,
                "target_account_id": "123456789012",
                "region": "us-east-1",
                "existing_cluster_name": "shared",
            }
        ),
        "shared_membership": binding.encode(),
    }


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "namespace",
        "organization",
        "workspace",
        "cluster",
        "region",
        "managed",
        "dedicated",
        "noncanonical",
    ],
)
def test_changed_membership_cannot_enter_dedicated_runtime(change):
    binding, parameters = request()
    org_id, workspace_id = binding.org_id, binding.workspace_id
    if change == "missing":
        parameters.pop("shared_membership")
    elif change == "namespace":
        value = json.loads(parameters["shared_membership"])
        value["namespace"] = "another-workspace"
        parameters["shared_membership"] = json.dumps(value)
    elif change == "organization":
        org_id = str(uuid4())
    elif change == "workspace":
        workspace_id = str(uuid4())
    elif change in {"cluster", "dedicated"}:
        value = json.loads(parameters["lifecycle_inputs"])
        value["shared_cluster_id" if change == "cluster" else "cluster_placement"] = (
            str(uuid4()) if change == "cluster" else "dedicated"
        )
        parameters["lifecycle_inputs"] = json.dumps(value)
    elif change == "noncanonical":
        parameters["shared_membership"] = json.dumps(
            json.loads(binding.encode()), indent=2
        )
    else:
        value = json.loads(parameters["lifecycle_request"])
        value["region" if change == "region" else "mode"] = (
            "us-west-2" if change == "region" else "existing-account-managed"
        )
        parameters["lifecycle_request"] = json.dumps(value)
    with pytest.raises(LifecycleRefused, match="identity is invalid"):
        approved_membership(parameters, org_id=org_id, workspace_id=workspace_id)


def test_original_shared_binding_and_historical_dedicated_inputs():
    binding, parameters = request()
    assert (
        approved_membership(
            parameters, org_id=binding.org_id, workspace_id=binding.workspace_id
        )
        == binding
    )
    assert (
        approved_membership(
            {"lifecycle_inputs": "{}"},
            org_id=binding.org_id,
            workspace_id=binding.workspace_id,
        )
        is None
    )
