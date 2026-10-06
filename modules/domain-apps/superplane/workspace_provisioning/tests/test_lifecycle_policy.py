"""Approval and worker normalize the same policy, including defaulted fields."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from workspace_provisioning.lifecycle_policy import (
    CredentialReference,
    LifecyclePolicy,
    policy_digest,
    policy_document,
)
from workspace_provisioning.runtime_config import (
    LifecycleRefused,
    supported_runtime_modes,
    validate_runtime_config,
)


def runtime_config():
    return {
        "version": 1,
        "backend": {
            "bucket": "fixture-state",
            "region": "us-west-2",
            "lock_table": "fixture-lock",
        },
        "environment": "dev",
        "workspace_variables": {},
        "actor_role_names": {
            "registrar": "registrar",
            "installer": "installer",
            "supervisor": "supervisor",
        },
        "namespace": "superplane-system",
        "enforce_version": "v1.31",
        "management_security_group_id": "sg-a123",
        "management_api_origin": "https://management.example.invalid",
        "bootstrap_credential_reference_id": "cred-fixture",
        "binaries": {
            name: "/opt/bin/" + name
            for name in ("python", "terraform", "kubectl", "aws")
        },
        "controller_image": "fixture/superplane-controller@sha256:" + "a" * 64,
        "imds_probe_image": "fixture/python@sha256:" + "b" * 64,
    }


@pytest.mark.parametrize(
    "invalid", [None, "broad", "overlap", "public", "host-bits", "ipv6", "extra"]
)
def test_hybrid_network_ranges_are_explicit_private_and_nonoverlapping(invalid):
    config = runtime_config()
    hybrid = {
        "node_cidr": "10.100.0.0/24",
        "pod_cidr": "10.101.0.0/16",
        "service_cidr": "172.20.0.0/16",
    }
    if invalid == "broad":
        hybrid["node_cidr"] = "10.0.0.0/8"
    elif invalid == "overlap":
        hybrid["pod_cidr"] = "10.100.0.0/16"
    elif invalid == "public":
        hybrid["node_cidr"] = "203.0.113.0/24"
    elif invalid == "host-bits":
        hybrid["node_cidr"] = "10.100.0.1/24"
    elif invalid == "ipv6":
        hybrid["node_cidr"] = "fd00::/64"
    elif invalid == "extra":
        hybrid["activation_code"] = "must-not-be-a-terraform-input"
    config["workspace_variables"]["hybrid_networks"] = hybrid
    if invalid:
        with pytest.raises(LifecycleRefused):
            validate_runtime_config(config)
    else:
        validated = validate_runtime_config(config)
        assert validated["workspace_variables"]["hybrid_networks"] == hybrid
        assert validated is not config


def policy():
    return {
        "adp_org_id": "adp-original",
        "aws_organization_id": "o-fixture1234",
        "management_account_id": "000000000001",
        "management_cluster": "management",
        "permitted_modes": ["managed", "adopt"],
        "permitted_target_accounts": ["000000000002"],
        "permitted_regions": ["us-west-2"],
        "isolation_modes": ["namespace", "dedicated"],
        "workspace_defaults": {},
        "operation_max_runtime_seconds": 900,
        "runtime": runtime_config(),
        "credential_references": {
            "000000000002": {
                "credential_id": "cred-fixture",
                "credential_service": "aws",
                "credential_label": "fixture",
            }
        },
    }


@pytest.mark.parametrize("network", ["owned", "supplied", None, "invalid"])
def test_supported_modes_only_advertise_executable_recipes(network):
    config = runtime_config()
    config["workspace_variables"]["networking_mode"] = network
    expected = {"adopt", "managed"} if network == "owned" else {"adopt"}
    assert supported_runtime_modes(config) == expected


def test_default_networking_supports_managed_and_adopt_only():
    assert supported_runtime_modes(runtime_config()) == {"managed", "adopt"}


def test_raw_file_and_api_model_hash_the_same_defaults_and_set_order():
    raw = policy()
    api = LifecyclePolicy.model_validate(raw)
    raw["permitted_modes"].reverse()
    raw["isolation_modes"].reverse()
    assert policy_digest(raw) == policy_digest(api)
    assert policy_document(raw)["permitted_organizational_units"] == []


@pytest.mark.parametrize(
    "field", ["runtime", "credential_references", "workspace_defaults", "adp_org_id"]
)
def test_changed_policy_cannot_reuse_preview_revision(field):
    original = policy()
    altered = deepcopy(original)
    if field == "runtime":
        altered[field]["actor_role_names"]["installer"] = "different-installer"
    elif field == "credential_references":
        altered[field]["000000000002"]["credential_id"] = "different-reference"
    elif field == "workspace_defaults":
        altered[field]["node_instance_type"] = "m6i.4xlarge"
    else:
        altered[field] = "adp-other"
    assert policy_digest(original) != policy_digest(altered)


@pytest.mark.parametrize(
    "reference",
    [
        "arn:aws:iam::123456789012:role/test",
        "arn%3Aaws",
        "some\u200breference",
        "reference with space",
        "AKIAIOSFODNN7EXAMPLE",
    ],
)
def test_policy_reference_cannot_contain_provider_location_or_secret(reference):
    with pytest.raises((ValidationError, ValueError)):
        CredentialReference(
            credential_id=reference,
            credential_service="aws",
            credential_label="fixture",
        )


@pytest.mark.parametrize(
    "change", ["target", "image", "role", "relative-binary", "origin"]
)
def test_unreviewable_runtime_is_refused(change):
    value = runtime_config()
    if change == "target":
        value["workspace_variables"]["account_id"] = "000000000003"
    elif change == "image":
        value["controller_image"] = "fixture/controller:latest"
    elif change == "role":
        value["actor_role_names"]["installer"] = []
    elif change == "relative-binary":
        value["binaries"]["aws"] = "aws"
    else:
        value["management_api_origin"] = "https://user:secret@example.invalid/"
    with pytest.raises(LifecycleRefused):
        validate_runtime_config(value)


@pytest.mark.parametrize(
    "boundary",
    [
        None,
        42,
        [],
        "reviewed-policy",
        "arn:aws:iam::aws:policy/AdministratorAccess",
        "arn:aws:iam::111122223333:policy/*",
        "arn:aws:iam::111122223333:role/role",
        "arn:aws:iam::111122223333:policy/owner//boundary",
    ],
)
def test_role_boundary_must_be_an_exact_customer_policy(boundary):
    config = runtime_config()
    config["workspace_variables"]["workspace_role_permissions_boundary_arn"] = boundary
    with pytest.raises(LifecycleRefused, match="workspace role boundary"):
        validate_runtime_config(config)


def test_role_boundary_is_preserved_and_bound_into_approved_policy():
    original = policy()
    configured = deepcopy(original)
    boundary = "arn:aws:iam::000000000002:policy/owner/workspace-services"
    configured["runtime"]["workspace_variables"][
        "workspace_role_permissions_boundary_arn"
    ] = boundary
    normalized = policy_document(configured)
    assert (
        normalized["runtime"]["workspace_variables"][
            "workspace_role_permissions_boundary_arn"
        ]
        == boundary
    )
    assert policy_digest(configured) != policy_digest(original)
    changed = deepcopy(configured)
    changed["runtime"]["workspace_variables"][
        "workspace_role_permissions_boundary_arn"
    ] = boundary + "-changed"
    assert policy_digest(changed) != policy_digest(configured)
    assert (
        "workspace_role_permissions_boundary_arn"
        not in original["runtime"]["workspace_variables"]
    )


def test_typed_installation_provider_handle_survives_policy_digest():
    from uuid import NAMESPACE_URL, uuid5

    original = policy()
    handle = "spda1:" + str(uuid5(NAMESPACE_URL, "review-only-role"))
    original["credential_references"]["000000000002"]["credential_id"] = handle
    original["runtime"]["workspace_variables"][
        "workspace_role_permissions_boundary_arn"
    ] = "arn:aws:iam::000000000002:policy/owner/workspace-services"
    api = LifecyclePolicy.model_validate(original)
    assert (
        policy_document(api)["credential_references"]["000000000002"]["credential_id"]
        == handle
    )
    assert policy_digest(original) == policy_digest(api)


@pytest.mark.parametrize(
    "reference",
    [
        "spda1:00000000-0000-4000-8000-000000000000",
        "spda1:00000000000050008000000000000000",
        "spda1:00000000-0000-5000-8000-00000000000A",
        "SPDA1:00000000-0000-5000-8000-000000000000",
        "spda1:not-a-uuid",
        "spda1:00000000-0000-5000-8000-000000000000/extra",
        "spda2:00000000-0000-5000-8000-000000000000",
    ],
)
def test_typed_installation_provider_handle_refuses_aliases(reference):
    with pytest.raises((ValidationError, ValueError)):
        CredentialReference(
            credential_id=reference,
            credential_service="aws",
            credential_label="fixture",
        )


@pytest.mark.parametrize(
    "alteration", ["missing", "empty", "foreign", "supplied", "vpc", "subnets"]
)
def test_installation_provider_cannot_admit_without_owned_network_and_boundary(
    alteration,
):
    from uuid import NAMESPACE_URL, uuid5

    configured = policy()
    configured["credential_references"]["000000000002"]["credential_id"] = (
        "spda1:" + str(uuid5(NAMESPACE_URL, "fixture-role"))
    )
    variables = configured["runtime"]["workspace_variables"]
    variables["workspace_role_permissions_boundary_arn"] = (
        "arn:aws:iam::000000000002:policy/owner/workspace-services"
    )
    if alteration == "missing":
        variables.pop("workspace_role_permissions_boundary_arn")
    elif alteration == "empty":
        variables["workspace_role_permissions_boundary_arn"] = ""
    elif alteration == "foreign":
        variables["workspace_role_permissions_boundary_arn"] = (
            "arn:aws:iam::000000000003:policy/foreign"
        )
    elif alteration == "supplied":
        variables["networking_mode"] = "supplied"
    elif alteration == "vpc":
        variables["supplied_vpc_id"] = "vpc-0123456789abcdef0"
    else:
        variables["supplied_private_subnet_ids"] = ["subnet-0123456789abcdef0"]
    with pytest.raises(
        ValidationError,
        match="reviewed same-account role boundary and owned networking",
    ):
        LifecyclePolicy.model_validate(configured)
