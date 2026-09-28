import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import workspace_kms as kms
from workspace_ownership import WorkspaceOwnershipError


ACCOUNT = "111122223333"
CALLER = f"arn:aws:iam::{ACCOUNT}:role/provisioner"
KEY = f"arn:aws:kms:us-east-1:{ACCOUNT}:key/11111111-2222-3333-4444-555555555555"
ASG = f"arn:aws:iam::{ACCOUNT}:role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling"


def requirement(sid, principal, actions, condition=None):
    result = {
        "Sid": sid,
        "Effect": "Allow",
        "Principal": principal,
        "Action": actions,
        "Resource": "*",
    }
    if condition:
        result["Condition"] = condition
    return result


@pytest.fixture
def preflight(monkeypatch):
    statements = [
        requirement(
            "AllowProvisioningCallerToConfigureEKSEncryption",
            {"AWS": CALLER},
            ["kms:DescribeKey", "kms:CreateGrant"],
        ),
        requirement(
            "Logs",
            {"Service": "logs.us-east-1.amazonaws.com"},
            [
                "kms:Encrypt",
                "kms:Decrypt",
                "kms:ReEncrypt*",
                "kms:GenerateDataKey*",
                "kms:DescribeKey",
            ],
            {
                "ArnEquals": {
                    "kms:EncryptionContext:aws:logs:arn": f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/eks/adp-dev-spw-alpha/cluster"
                }
            },
        ),
        requirement(
            "AutoScalingUse",
            {"AWS": ASG},
            [
                "kms:Encrypt",
                "kms:Decrypt",
                "kms:ReEncrypt*",
                "kms:GenerateDataKey*",
                "kms:DescribeKey",
            ],
            {"StringEquals": {"kms:ViaService": "ec2.us-east-1.amazonaws.com"}},
        ),
        requirement(
            "AutoScalingGrant",
            {"AWS": ASG},
            ["kms:CreateGrant"],
            {"Bool": {"kms:GrantIsForAWSResource": "true"}},
        ),
    ]
    policy = {"Version": "2012-10-17", "Statement": statements}
    plan = {
        "variables": {
            "kms_key_arn": {"value": KEY},
            "account_id": {"value": ACCOUNT},
            "aws_region": {"value": "us-east-1"},
        },
        "planned_values": {
            "outputs": {
                "provisioning_principal_arn": {"value": CALLER},
                "supplied_kms_key_required_policy": {
                    "value": json.dumps(
                        {"key_arn": KEY, "required_statements": statements}
                    )
                },
                "provisioning_caller_kms_requirements": {
                    "value": {"principal_arn": CALLER}
                },
            }
        },
    }
    data = {
        "key": {
            "Arn": KEY,
            "Enabled": True,
            "KeyState": "Enabled",
            "KeyManager": "CUSTOMER",
            "KeyUsage": "ENCRYPT_DECRYPT",
            "KeySpec": "SYMMETRIC_DEFAULT",
        },
        "policy": copy.deepcopy(policy),
        "simulation": [
            {"EvalActionName": action, "EvalDecision": "allowed"}
            for action in ("kms:DescribeKey", "kms:CreateGrant")
        ],
        "dry_error": "DryRunOperationException",
        "dry_exit": 255,
        "calls": [],
    }

    def aws(service, operation, *flags, region):
        data["calls"].append((service, operation))
        if operation == "get-caller-identity":
            return {
                "Account": ACCOUNT,
                "Arn": data.get(
                    "session_arn",
                    f"arn:aws:sts::{ACCOUNT}:assumed-role/provisioner/session",
                ),
            }
        if operation == "get-role":
            assert "/" not in flags[flags.index("--role-name") + 1]
            return {"Role": {"Arn": data.get("caller_arn", CALLER)}}
        if operation == "describe-key":
            if data.get("missing"):
                raise WorkspaceOwnershipError("Key does not exist")
            return {"KeyMetadata": data["key"]}
        if operation == "get-key-policy":
            return {"Policy": json.dumps(data["policy"])}
        if operation == "simulate-principal-policy":
            return {"EvaluationResults": data["simulation"]}
        raise AssertionError(operation)

    def dry_run(argv, **kwargs):
        assert argv[:3] == ["aws", "kms", "create-grant"]
        assert "--dry-run" in argv
        data["calls"].append(("kms", "create-grant-dry-run"))
        return SimpleNamespace(returncode=data["dry_exit"], stderr=data["dry_error"])

    monkeypatch.setattr(kms, "_aws", aws)
    monkeypatch.setattr(kms.subprocess, "run", dry_run)
    return plan, data


def test_supplied_key_checks_live_metadata_policy_identity_and_dry_run(preflight):
    plan, data = preflight
    receipt = kms.verify_supplied_key(plan)
    assert receipt["key_arn"] == KEY and receipt["create_grant_dry_run"]
    assert data["calls"][-1] == ("kms", "create-grant-dry-run")
    assert (
        kms.verify_supplied_key(plan, approved=receipt)["policy_sha256"]
        == receipt["policy_sha256"]
    )
    assert data["calls"].count(("kms", "describe-key")) == 2


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "disabled",
        "deleting",
        "asymmetric",
        "aws-managed",
        "caller",
        "logs",
        "autoscaling",
        "deny",
        "context",
        "identity",
        "boundary",
        "organization",
        "dry-denied",
        "dry-success-response",
    ],
)
def test_unusable_or_unverified_supplied_keys_deny(preflight, problem):
    plan, data = preflight
    if problem == "missing":
        data["missing"] = True
    elif problem == "disabled":
        data["key"]["Enabled"] = False
    elif problem == "deleting":
        data["key"]["KeyState"] = "PendingDeletion"
    elif problem == "asymmetric":
        data["key"]["KeySpec"] = "RSA_2048"
    elif problem == "aws-managed":
        data["key"]["KeyManager"] = "AWS"
    elif problem in ("caller", "logs", "autoscaling"):
        del data["policy"]["Statement"][
            {"caller": 0, "logs": 1, "autoscaling": 3}[problem]
        ]
    elif problem == "deny":
        data["policy"]["Statement"].append({"Effect": "Deny"})
    elif problem == "context":
        data["simulation"][0]["MissingContextValues"] = ["kms:ViaService"]
    elif problem == "identity":
        data["simulation"][0]["EvalDecision"] = "implicitDeny"
    elif problem == "boundary":
        data["simulation"][0]["PermissionsBoundaryDecisionDetail"] = {
            "AllowedByPermissionsBoundary": False
        }
    elif problem == "organization":
        data["simulation"][0]["OrganizationsDecisionDetail"] = {
            "AllowedByOrganizations": False
        }
    elif problem == "dry-denied":
        data["dry_error"] = "AccessDeniedException"
    else:
        data["dry_exit"] = 0
    with pytest.raises(WorkspaceOwnershipError):
        kms.verify_supplied_key(plan)


def test_changed_key_policy_invalidates_review_evidence(preflight):
    plan, data = preflight
    receipt = kms.verify_supplied_key(plan)
    data["policy"]["Statement"][0]["Sid"] = "ChangedPolicyAfterReview"
    with pytest.raises(WorkspaceOwnershipError, match="changed after plan review"):
        kms.verify_supplied_key(plan, approved=receipt)


def test_owned_key_plan_performs_no_supplied_key_calls(monkeypatch):
    monkeypatch.setattr(
        kms, "_aws", lambda *a, **kw: pytest.fail("Unexpected AWS call")
    )
    assert (
        kms.verify_supplied_key({"variables": {"kms_key_arn": {"value": ""}}}) is None
    )


def test_preflight_consumes_complete_terraform_rendered_contract(preflight):
    # Generated by Terraform 1.9.8 -verbose; the Terraform test asserts exact equality
    # against this fixture so the producer and consumer cannot silently drift.
    plan, data = preflight
    rendered = json.loads(
        (
            Path(__file__).parent / "fixtures/supplied-kms-required-policy.json"
        ).read_text()
    )
    caller = next(
        s["Principal"]["AWS"]
        for s in rendered["required_statements"]
        if "ProvisioningCaller" in s["Sid"]
    )
    data["caller_arn"] = caller
    data["policy"] = {
        "Version": "2012-10-17",
        "Statement": rendered["required_statements"],
    }
    outputs = plan["planned_values"]["outputs"]
    outputs["supplied_kms_key_required_policy"]["value"] = json.dumps(rendered)
    outputs["provisioning_caller_kms_requirements"]["value"]["principal_arn"] = caller
    outputs["provisioning_principal_arn"]["value"] = caller
    assert kms.verify_supplied_key(plan)["principal_arn"] == caller


@pytest.mark.parametrize("problem", ["missing", "empty", "wrong-key"])
def test_malformed_supplied_policy_contract_denies(preflight, problem):
    plan, data = preflight
    value = plan["planned_values"]["outputs"]["supplied_kms_key_required_policy"]
    required = json.loads(value["value"])
    if problem == "missing":
        required.pop("required_statements")
    elif problem == "empty":
        required["required_statements"] = []
    else:
        required["key_arn"] = "other-key"
    value["value"] = json.dumps(required)
    with pytest.raises(WorkspaceOwnershipError, match="rendered required-statements"):
        kms.verify_supplied_key(plan)


@pytest.mark.parametrize("problem", ["account", "role", "unavailable"])
def test_account_prerequisites_refuse_wrong_or_unavailable_identity(
    monkeypatch, problem
):
    def aws(service, operation, *flags, region):
        if operation == "get-caller-identity":
            return {"Account": "wrong" if problem == "account" else ACCOUNT}
        if problem == "unavailable":
            raise WorkspaceOwnershipError("Unavailable")
        return {"Role": {"Arn": "wrong" if problem == "role" else ASG}}

    monkeypatch.setattr(kms, "_aws", aws)
    with pytest.raises(WorkspaceOwnershipError):
        kms.verify_account_prerequisites(
            {"account_id": ACCOUNT, "aws_region": "us-east-1"}
        )


def test_owned_key_caller_is_known_despite_unknown_key_dependent_output(preflight):
    plan, data = preflight
    plan["variables"]["kms_key_arn"]["value"] = ""
    # Real Terraform omits the composite value while the newly-created key is unknown.
    plan["planned_values"]["outputs"]["provisioning_caller_kms_requirements"] = {
        "sensitive": False
    }
    receipt = kms.verify_provisioning_principal(plan)
    assert receipt == {"account_id": ACCOUNT, "principal_arn": CALLER}
    assert all(service != "kms" for service, _ in data["calls"])
    data["caller_arn"] = f"arn:aws:iam::{ACCOUNT}:role/other-provisioner"
    with pytest.raises(WorkspaceOwnershipError, match="principal"):
        kms.verify_provisioning_principal(plan, approved=receipt)


@pytest.mark.parametrize(
    "session,path,accepted",
    [
        ("first", "path/to", True),
        ("second", "path/to", True),
        ("first", "another/path", False),
    ],
)
def test_path_qualified_roles_use_terminal_getrole_name_and_compare_full_arn(
    preflight, session, path, accepted
):
    plan, data = preflight
    plan["variables"]["kms_key_arn"]["value"] = ""
    plan["planned_values"]["outputs"]["provisioning_principal_arn"]["value"] = (
        f"arn:aws:iam::{ACCOUNT}:role/path/to/provisioner"
    )
    data["session_arn"] = (
        f"arn:aws:sts::{ACCOUNT}:assumed-role/{path}/provisioner/{session}"
    )
    data["caller_arn"] = f"arn:aws:iam::{ACCOUNT}:role/{path}/provisioner"
    if accepted:
        assert (
            kms.verify_provisioning_principal(plan)["principal_arn"]
            == data["caller_arn"]
        )
    else:
        with pytest.raises(WorkspaceOwnershipError, match="principal"):
            kms.verify_provisioning_principal(plan)
