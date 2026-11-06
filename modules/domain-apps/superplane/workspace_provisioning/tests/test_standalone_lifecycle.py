"""Standalone preview survives worker validation with exact policy/target binding."""

from dataclasses import asdict, replace
import json
from types import SimpleNamespace

import pytest

from account_factory.modes import ModeError, OwnershipMode, ValidationAuthorization
from workspace_provisioning.artifacts import canonical, digest
from workspace_provisioning.authority import validated_request
from workspace_provisioning.lifecycle_policy import policy_digest
from workspace_provisioning.preview import preview_workspace
from workspace_provisioning.runtime_config import LifecycleRefused

from .test_lifecycle_policy import standalone_policy
from .test_preview import request_for


def admitted(tmp_path, mode):
    policy = standalone_policy()
    account = policy["management_account_id"]
    request = replace(
        request_for(mode),
        organization_id="standalone",
        target_account_id=account,
        management_cluster=policy["management_cluster"],
    )
    authorization = ValidationAuthorization(
        organization_id="standalone",
        operation_org_id="domain-org",
        management_account_id=account,
        management_cluster=policy["management_cluster"],
        workspace_id=request.workspace_id,
        permitted_modes=frozenset(
            {
                OwnershipMode.EXISTING_ACCOUNT_MANAGED,
                OwnershipMode.BRING_EXISTING_CLUSTER,
            }
        ),
        permitted_target_accounts=frozenset({account}),
        permitted_organizational_units=frozenset(),
    )
    inputs = {"isolation_mode": "dedicated", "cluster_placement": "dedicated"}
    preview = preview_workspace(
        request,
        authorization=authorization,
        requested_capacity={
            "max_resource_units": 0,
            "max_runtime_seconds": 900,
            "max_cost_micros": 20000000,
            "request": inputs,
            "allocation_id": "allocation-fixture",
            "policy_revision": policy_digest(policy),
        },
        cost_estimate=None,
        approval_required=True,
    )
    parameters = {
        "lifecycle_policy_sha256": policy_digest(policy),
        "runtime_config_sha256": digest(policy["runtime"]),
        "lifecycle_request": canonical(asdict(request)),
        "lifecycle_inputs": canonical(inputs),
        "lifecycle_allocation_max_resource_units": "0",
        "lifecycle_allocation_max_runtime_seconds": "900",
        "lifecycle_allocation_max_cost_micros": "20000000",
        "allocation_id": "allocation-fixture",
        "plan_revision": preview.revision,
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": 1, "tenants": {"domain-org": policy}}))
    operation = SimpleNamespace(
        request=SimpleNamespace(parameters=parameters),
        grant=SimpleNamespace(
            lease=SimpleNamespace(
                org_id="domain-org",
                workspace_id=request.workspace_id,
            )
        ),
    )
    return operation, SimpleNamespace(policy_file=path), policy


@pytest.mark.parametrize(
    "mode",
    [
        OwnershipMode.EXISTING_ACCOUNT_MANAGED,
        OwnershipMode.BRING_EXISTING_CLUSTER,
    ],
)
def test_standalone_worker_revalidates_the_exact_preview(tmp_path, mode):
    operation, context, _ = admitted(tmp_path, mode)
    _, request, authorization = validated_request(operation, context)
    assert request.organization_id == authorization.organization_id == "standalone"
    assert request.target_account_id == authorization.management_account_id
    assert authorization.operation_org_id == "domain-org"


@pytest.mark.parametrize("changed", ["target", "organization", "new-account", "policy"])
def test_standalone_worker_refuses_changed_admission_or_policy(tmp_path, changed):
    operation, context, policy = admitted(
        tmp_path, OwnershipMode.EXISTING_ACCOUNT_MANAGED
    )
    parameters = operation.request.parameters
    request = json.loads(parameters["lifecycle_request"])
    if changed == "target":
        request["target_account_id"] = "000000000002"
    elif changed == "organization":
        request["organization_id"] = "o-fixture1234"
    elif changed == "new-account":
        request.update(
            mode="new-account-managed",
            target_account_id=None,
            account_email="fixture@example.invalid",
            organizational_unit_id="ou-test-fixture01",
        )
    else:
        policy["aws_organization_id"] = "o-fixture1234"
        context.policy_file.write_text(
            json.dumps({"version": 1, "tenants": {"domain-org": policy}})
        )
    parameters["lifecycle_request"] = canonical(request)
    with pytest.raises((ModeError, LifecycleRefused)):
        validated_request(operation, context)
