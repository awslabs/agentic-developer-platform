"""Source-role qualification checks the whole trust policy and S3 Region aliases."""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from src.l5_backend_well_architected import qualify_source_role


@pytest.fixture
def source_role():
    account = "000000000000"
    execution_role = f"arn:aws:iam::{account}:role/SourceReader"
    identity = {"account_id": account, "region": "us-east-1", "role_arn": f"arn:aws:iam::{account}:role/Broker"}
    settings = {
        "well_architected": {
            "review_mode": "source_only",
            "pillars": ["SECURITY"],
            "execution_role_ref": {"record_type": "role", "record_id": execution_role},
            "source_store_ref": {"record_type": "artifact_store", "record_id": "example-source-bucket"},
        }
    }
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {"Service": "wellarchitected.amazonaws.com"},
                "Condition": {"StringEquals": {"aws:SourceAccount": account}},
            }
        ],
    }
    provider = MagicMock()
    native = provider.client.return_value
    native.get_role.return_value = {"Role": {"Arn": execution_role, "AssumeRolePolicyDocument": trust}}
    native.get_bucket_location.return_value = {"LocationConstraint": None}
    native.simulate_principal_policy.return_value = {
        "EvaluationResults": [
            {
                "EvalActionName": "s3:GetObjectVersion",
                "EvalResourceName": "arn:aws:s3:::example-source-bucket/l5/well_architected/work/*",
                "EvalDecision": "allowed",
                "MissingContextValues": [],
            }
        ]
    }
    return provider, settings, identity, trust


@pytest.mark.parametrize("representation", ["list", "statement", "principal_and_action_lists", "profile_condition"])
def test_accepts_only_constrained_source_role_trust(source_role, representation):
    provider, settings, identity, trust = source_role
    statement = trust["Statement"][0]
    if representation == "statement":
        trust["Statement"] = statement
    elif representation == "principal_and_action_lists":
        statement["Action"] = [statement["Action"]]
        statement["Principal"]["Service"] = [statement["Principal"]["Service"]]
    elif representation == "profile_condition":
        statement["Condition"]["ArnLike"] = {
            "aws:SourceArn": f"arn:aws:wellarchitected:{identity['region']}:{identity['account_id']}:agent-profile/*"
        }
    qualification = qualify_source_role(provider, settings, identity)
    assert qualification["trust"] == "qualified"
    assert qualification["source_permissions"] == "qualified"
    assert qualification["execution_role_arn"] == settings["well_architected"]["execution_role_ref"]["record_id"]
    provider.client.return_value.describe_services.assert_called_once_with()


@pytest.mark.parametrize(
    "grant",
    [
        {"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": {"AWS": "*"}},
        {"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": {"Service": "wellarchitected.amazonaws.com"}},
        {"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": {"Service": "ec2.amazonaws.com"}},
    ],
)
def test_rejects_additional_unrestricted_trust_grants(source_role, grant):
    provider, settings, identity, trust = source_role
    trust["Statement"].append(grant)
    with pytest.raises(ValueError, match="trust"):
        qualify_source_role(provider, settings, identity)
    native = provider.client.return_value
    native.get_bucket_location.assert_not_called()
    native.simulate_principal_policy.assert_not_called()
    native.describe_services.assert_not_called()


@pytest.mark.parametrize("change", ["principal_key", "extra_action", "not_principal", "wrong_account", "unsupported_grant", "empty"])
def test_rejects_unsupported_trust_permissions(source_role, change):
    provider, settings, identity, trust = source_role
    statement = trust["Statement"][0]
    if change == "principal_key":
        statement["Principal"]["AWS"] = "*"
    elif change == "extra_action":
        statement["Action"] = ["sts:AssumeRole", "sts:TagSession"]
    elif change == "not_principal":
        statement["NotPrincipal"] = {"AWS": "arn:aws:iam::000000000001:root"}
    elif change == "wrong_account":
        statement["Condition"]["StringEquals"]["aws:SourceAccount"] = "000000000001"
    elif change == "unsupported_grant":
        unsupported = deepcopy(statement)
        unsupported["Effect"] = "Deny"
        trust["Statement"].append(unsupported)
    else:
        trust["Statement"] = []
    with pytest.raises(ValueError, match="trust"):
        qualify_source_role(provider, settings, identity)
    provider.client.return_value.describe_services.assert_not_called()


@pytest.mark.parametrize(
    ("location", "region"),
    [
        (None, "us-east-1"),
        ("us-east-1", "us-east-1"),
        ("EU", "eu-west-1"),
        ("eu-west-1", "eu-west-1"),
        ("ap-southeast-2", "ap-southeast-2"),
    ],
)
def test_normalizes_same_region_s3_locations(source_role, location, region):
    provider, settings, identity, _ = source_role
    identity["region"] = region
    provider.client.return_value.get_bucket_location.return_value = {"LocationConstraint": location}
    assert qualify_source_role(provider, settings, identity)["region"] == region


@pytest.mark.parametrize(("location", "region"), [("EU", "us-east-1"), (None, "eu-west-1"), ("us-west-2", "eu-west-1")])
def test_rejects_different_s3_region(source_role, location, region):
    provider, settings, identity, _ = source_role
    identity["region"] = region
    native = provider.client.return_value
    native.get_bucket_location.return_value = {"LocationConstraint": location}
    with pytest.raises(ValueError, match="share a Region"):
        qualify_source_role(provider, settings, identity)
    native.simulate_principal_policy.assert_not_called()
    native.describe_services.assert_not_called()
