"""Real AWS validator decisions at controlled SDK boundaries; no live resources."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from src.auth.provider_validation import AwsEc2Validator, AwsValidationProfile, ValidationUnavailableError, configured_validator
from src.internal.sts_assume_service import STSAssumeError

PROFILE = dict(
    region="us-east-1", image_id="ami-1234abcd", instance_type="g5.xlarge", subnet_id="subnet-1234abcd", security_group_ids=["sg-1234abcd"]
)
MATERIAL = json.dumps({"role_arn": "arn:aws:iam::123456789012:role/validated"})


@pytest.fixture
def provider(monkeypatch):
    assume = MagicMock(return_value=SimpleNamespace(access_key_id="testing", secret_access_key="testing", session_token="testing"))
    monkeypatch.setattr("src.auth.provider_validation.assume_role", assume)
    clients = {service: MagicMock() for service in ("ec2", "sts", "service-quotas")}
    clients["sts"].get_caller_identity.return_value = {"Account": "123456789012"}
    clients["ec2"].run_instances.side_effect = ClientError({"Error": {"Code": "DryRunOperation"}}, "RunInstances")
    clients["service-quotas"].get_service_quota.return_value = {"Quota": {"Value": 8.0}}
    pages = {"describe_instances": [{"Reservations": []}], "describe_capacity_reservations": [{"CapacityReservations": []}]}
    clients["ec2"].get_paginator.side_effect = lambda operation: SimpleNamespace(paginate=lambda **kwargs: iter(pages[operation]))
    clients["ec2"].describe_instance_types.side_effect = lambda **kwargs: {
        "InstanceTypes": [{"InstanceType": kind, "VCpuInfo": {"DefaultVCpus": 4}} for kind in kwargs["InstanceTypes"]]
    }
    factory = MagicMock(return_value=SimpleNamespace(client=lambda service, **kwargs: clients[service]))
    validator = AwsEc2Validator(AwsValidationProfile(**PROFILE), session_factory=factory)
    return SimpleNamespace(validator=validator, clients=clients, pages=pages, assume=assume)


def validate(provider):
    return provider.validator.validate(MATERIAL, credential_type="aws_role", user_id="owner", label="default")


def test_permission_proof_is_always_a_dry_run_and_capacity_is_not_invented(provider):
    reading = validate(provider)
    assert (reading.credential_valid, reading.permissions_sufficient, reading.quota_available, reading.observed_capacity) == (True, True, True, None)
    assert provider.clients["ec2"].run_instances.call_args.kwargs["DryRun"] is True
    assert provider.assume.call_args.kwargs["user_id"] == "owner"


def test_quota_includes_only_matching_ondemand_family_and_unused_owned_reservations(provider):
    provider.pages["describe_instances"] = [
        {
            "Reservations": [
                {
                    "Instances": [
                        {"InstanceType": "g5.xlarge"},
                        {"InstanceType": "g5.xlarge", "InstanceLifecycle": "spot"},
                        {"InstanceType": "p4d.24xlarge"},
                    ]
                }
            ]
        }
    ]
    assert validate(provider).quota_available is True
    provider.pages["describe_capacity_reservations"] = [
        {
            "CapacityReservations": [
                {"OwnerId": "123456789012", "State": "active", "InstanceType": "g5.xlarge", "AvailableInstanceCount": 1},
            ]
        }
    ]
    assert validate(provider).quota_available is False


@pytest.mark.parametrize(
    "instance_type,code", [("g6f.xlarge", "L-DB2E81BA"), ("vt1.3xlarge", "L-DB2E81BA"), ("p5.48xlarge", "L-417A185B"), ("m7i.large", "L-1216C47")]
)
def test_uses_the_correct_regional_vcpu_quota(provider, instance_type, code):
    provider.validator.profile = AwsValidationProfile(**(PROFILE | {"instance_type": instance_type}))
    assert validate(provider).quota_available is True
    assert provider.clients["service-quotas"].get_service_quota.call_args.kwargs["QuotaCode"] == code


@pytest.mark.parametrize(
    "failure",
    [
        "quota_nan",
        "quota_bool",
        "quota_missing",
        "types_missing",
        "invalid_vcpus",
        "invalid_reservation",
        "inventory_bound",
        "dry_run_success",
        "dry_run_network",
        "identity_missing",
    ],
)
def test_malformed_or_unavailable_observations_cannot_become_positive_evidence(provider, failure):
    ec2 = provider.clients["ec2"]
    if failure.startswith("quota_"):
        provider.clients["service-quotas"].get_service_quota.return_value = (
            {"Quota": {"Value": float("nan") if failure == "quota_nan" else True}} if failure != "quota_missing" else {}
        )
    elif failure in {"types_missing", "invalid_vcpus"}:
        ec2.describe_instance_types.side_effect = None
        ec2.describe_instance_types.return_value = {
            "InstanceTypes": [] if failure == "types_missing" else [{"InstanceType": "g5.xlarge", "VCpuInfo": {"DefaultVCpus": True}}]
        }
    elif failure == "invalid_reservation":
        provider.pages["describe_capacity_reservations"] = [
            {"CapacityReservations": [{"OwnerId": "123456789012", "State": "active", "InstanceType": "g5.xlarge", "AvailableInstanceCount": -1}]}
        ]
    elif failure == "inventory_bound":
        provider.pages["describe_instances"] = [{"Reservations": []}] * 101
    elif failure == "dry_run_success":
        ec2.run_instances.side_effect = None
    elif failure == "dry_run_network":
        ec2.run_instances.side_effect = EndpointConnectionError(endpoint_url="https://provider.invalid/secret-sentinel")
    else:
        provider.clients["sts"].get_caller_identity.return_value = {}
    with pytest.raises(ValidationUnavailableError) as caught:
        validate(provider)
    assert "secret-sentinel" not in str(caught.value)


def test_denied_role_does_not_continue_provider_calls(provider):
    provider.assume.side_effect = STSAssumeError("secret-sentinel", "AccessDenied")
    reading = validate(provider)
    assert (reading.credential_valid, reading.permissions_sufficient, reading.quota_available) == (False, False, False)
    provider.clients["ec2"].run_instances.assert_not_called()
    assert "secret-sentinel" not in reading.detail


@pytest.mark.parametrize("mutation", ["tenant", "workspace", "provider", "unsupported_family", "malformed", "security_group"])
def test_only_server_configured_supported_workspace_profiles_are_usable(mutation):
    profile = PROFILE | ({"instance_type": "mac2.metal"} if mutation == "unsupported_family" else {})
    if mutation == "security_group":
        profile["security_group_ids"] = ["not-a-security-group"]
    config = SimpleNamespace(credential_validation_profiles="not-json" if mutation == "malformed" else json.dumps({"org": {"workspace": profile}}))
    with pytest.raises(ValidationUnavailableError):
        configured_validator(
            config,
            org_id="other" if mutation == "tenant" else "org",
            workspace_id="other" if mutation == "workspace" else "workspace",
            service="gcp" if mutation == "provider" else "aws",
        )
