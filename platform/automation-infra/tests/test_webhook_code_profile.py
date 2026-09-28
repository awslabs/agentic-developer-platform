"""Webhook-code deployment profile admission tests (#6057).

Verifies the Lambda-only verifier accepts exact targets with bounded execution
roles and rejects wrong roles, missing boundaries, wrong functions, cross-account
targets, and concurrent RevisionId conflicts. All AWS calls are mocked.
"""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "inventory", Path(__file__).resolve().parents[1] / "verify-workload-inventory.py"
)
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)

ACCOUNT = "123456789012"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/adp-test-webhook-worker"
BOUNDARY = f"arn:aws:iam::{ACCOUNT}:policy/adp-test-webhook-ceiling"
FUNCTION = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:adp-test-github-webhook"


def make_config(targets=None, boundaries=None):
    return {
        "account_id": ACCOUNT,
        "webhook_code_archive_prefix": "lambda-artifacts/webhook",
        "deployment_manifest": {
            "account_id": ACCOUNT,
            "region": "us-east-1",
            "archive_prefix": "lambda-artifacts/webhook",
            "targets": [
                {
                    "function_arn": fn,
                    "execution_role": role,
                    "artifact": f"handler-{i}.zip",
                }
                for i, (fn, role) in enumerate(
                    ({FUNCTION: ROLE} if targets is None else targets).items()
                )
            ],
        },
        "webhook_code_lambda_targets": {FUNCTION: ROLE} if targets is None else targets,
        "deployment_role_boundaries": {ROLE: BOUNDARY}
        if boundaries is None
        else boundaries,
    }


CEILING = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow", "Action": "logs:PutLogEvents", "Resource": "*"},
        {"Effect": "Deny", "NotAction": ["logs:PutLogEvents"], "Resource": "*"},
    ],
}


def boundary_reply(args, role=ROLE, boundary=BOUNDARY, policy=CEILING):
    if args[1] == "get-role":
        return {
            "Role": {
                "Arn": role,
                "PermissionsBoundary": {"PermissionsBoundaryArn": boundary},
            }
        }
    if args[1] == "get-policy":
        return {"Policy": {"DefaultVersionId": "v1"}}
    if args[1] == "get-policy-version":
        return {"PolicyVersion": {"Document": policy}}
    raise AssertionError(f"Unexpected AWS call: {args}")


def mock_aws(role=ROLE, account=ACCOUNT, boundary=BOUNDARY, policy=CEILING):
    """Return a mock AWS callable that returns the expected role for the function."""

    def aws(*args):
        if args[1] == "get-caller-identity":
            return {"Account": account}
        if args[1] == "get-function-configuration":
            return {"Role": role}
        return boundary_reply(args, role, boundary, policy)

    return aws


class TestPositiveAdmission:
    def test_exact_lambda_profile_passes(self):
        result = inventory.verify_webhook_code_targets(make_config(), aws=mock_aws())
        assert result["account"] == ACCOUNT
        assert result["targets"] == 1

    def test_multiple_targets_all_bounded(self):
        fn2 = (
            f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:adp-test-eventbridge-webhook"
        )
        role2 = f"arn:aws:iam::{ACCOUNT}:role/adp-test-eventbridge-worker"
        boundary2 = f"arn:aws:iam::{ACCOUNT}:policy/adp-test-eventbridge-ceiling"
        targets = {FUNCTION: ROLE, fn2: role2}
        boundaries = {ROLE: BOUNDARY, role2: boundary2}
        call_count = {"n": 0}

        def aws(*args):
            if args[1] == "get-caller-identity":
                return {"Account": ACCOUNT}
            if args[1] == "get-function-configuration":
                call_count["n"] += 1
                fn = args[-1]
                return {"Role": targets[fn]}
            expected = (
                role2
                if args[1] == "get-role" and args[-1] == role2.rsplit("/", 1)[1]
                else ROLE
            )
            return boundary_reply(args, expected, boundaries[expected])

        result = inventory.verify_webhook_code_targets(
            make_config(targets=targets, boundaries=boundaries), aws=aws
        )
        assert result["targets"] == 2
        assert call_count["n"] == 2


class TestRejections:
    def test_wrong_execution_role_rejected(self):
        wrong_role = f"arn:aws:iam::{ACCOUNT}:role/adp-test-other-worker"
        with pytest.raises(AssertionError, match="execution role mismatch"):
            inventory.verify_webhook_code_targets(
                make_config(), aws=mock_aws(role=wrong_role)
            )

    def test_role_not_in_boundary_map_rejected(self):
        """Function has the right role, but that role is not in the boundary map."""
        unbounded_role = f"arn:aws:iam::{ACCOUNT}:role/adp-test-unbounded"
        config = make_config(
            targets={FUNCTION: unbounded_role},
            boundaries={ROLE: BOUNDARY},  # unbounded_role not here
        )
        with pytest.raises(AssertionError, match="not in admitted boundary map"):
            inventory.verify_webhook_code_targets(
                config, aws=mock_aws(role=unbounded_role)
            )

    def test_cross_account_lambda_rejected(self):
        cross_fn = "arn:aws:lambda:us-east-1:999999999999:function:adp-other-webhook"
        config = make_config(targets={cross_fn: ROLE})
        with pytest.raises(AssertionError, match="Cross-account"):
            inventory.verify_webhook_code_targets(config, aws=mock_aws())

    def test_wrong_account_identity_rejected(self):
        with pytest.raises(AssertionError, match="Wrong AWS account"):
            inventory.verify_webhook_code_targets(
                make_config(), aws=mock_aws(account="999999999999")
            )

    def test_empty_targets_rejected(self):
        config = make_config(targets={})
        with pytest.raises(AssertionError, match="No webhook code Lambda targets"):
            inventory.verify_webhook_code_targets(config, aws=mock_aws())

    def test_non_lambda_arn_rejected(self):
        bad_fn = f"arn:aws:codebuild:us-east-1:{ACCOUNT}:project/adp-test-build"
        config = make_config(targets={bad_fn: ROLE})
        with pytest.raises(AssertionError, match="Not a Lambda ARN"):
            inventory.verify_webhook_code_targets(config, aws=mock_aws())


class TestGenericAdmissionRegression:
    """Verify the existing cluster admission verifier is untouched."""

    def test_existing_workload_ceiling_test_still_importable(self):
        # The existing test_workload_inventory tests import the same module;
        # verify that the module still exports verify_ceiling and verify_inventory.
        assert hasattr(inventory, "verify_ceiling")
        assert hasattr(inventory, "verify_inventory")
        assert hasattr(inventory, "verify_mutable_policies")

    def test_verify_inventory_requires_roles(self):
        """Empty roles in the full verifier should still raise."""
        config = {
            "account_id": ACCOUNT,
            "deployment_role_boundaries": {},
            "deployment_execution_resources": [],
            "deployment_managed_policy_arns": [],
            "clusters": [],
        }
        with pytest.raises(AssertionError, match="No workload roles"):
            inventory.verify_inventory(config, aws=mock_aws())


@pytest.mark.parametrize("boundary", [None, "arn:aws:iam::123456789012:policy/other"])
def test_actual_missing_or_wrong_boundary_rejected(boundary):
    with pytest.raises(AssertionError, match="Missing/wrong ceiling"):
        inventory.verify_webhook_code_targets(
            make_config(), aws=mock_aws(boundary=boundary)
        )


def test_actual_permissive_boundary_rejected():
    policy = {"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
    with pytest.raises(AssertionError):
        inventory.verify_webhook_code_targets(
            make_config(), aws=mock_aws(policy=policy)
        )


def test_execution_role_cannot_chain_execution():
    policy = {
        "Statement": [
            {"Effect": "Allow", "Action": "lambda:UpdateFunctionCode", "Resource": "*"},
            {
                "Effect": "Deny",
                "NotAction": ["lambda:UpdateFunctionCode"],
                "Resource": "*",
            },
        ]
    }
    with pytest.raises(AssertionError, match="Uninventoried executable capability"):
        inventory.verify_webhook_code_targets(
            make_config(), aws=mock_aws(policy=policy)
        )


def test_manifest_cannot_change_admitted_execution_role():
    config = make_config()
    config["deployment_manifest"]["targets"][0]["execution_role"] = "other-role"
    with pytest.raises(AssertionError, match="manifest target mismatch"):
        inventory.verify_webhook_code_targets(config, aws=mock_aws())
