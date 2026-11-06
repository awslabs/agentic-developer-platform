"""Offline evidence for the retained evaluator's read-only rollout audit."""

import runpy
from copy import deepcopy
from pathlib import Path

import pytest

PREFLIGHT = runpy.run_path(
    str(
        Path(__file__).resolve().parents[3]
        / "platform/evaluator-infra/legacy-existing/preflight.py"
    )
)
verify = PREFLIGHT["verify"]
REGISTRY_ID = PREFLIGHT["REGISTRY_ID"]
RECOVERY_REGISTRY_ID = PREFLIGHT["RECOVERY_REGISTRY_ID"]
VIEW = PREFLIGHT["VIEW"]


@pytest.fixture
def installed():
    account = "000000000101"
    region = "us-east-1"
    orchestrator = "adp-cli-uplift-eval-orchestrator"
    instance = "adp-cli-uplift-eval-instance"
    roles = {
        name: {
            "Arn": f"arn:aws:iam::{account}:role/{name}",
            "RoleId": f"role-id-{name}",
            "Tags": [
                {"Key": key, "Value": value}
                for key, value in (
                    PREFLIGHT["TAGS"]
                    if name == instance
                    else {"Purpose": "cli-uplift-eval"}
                ).items()
            ],
        }
        for name in (orchestrator, instance)
    }
    config = {
        "account": account,
        "region": region,
        "orchestrator_role": orchestrator,
        "instance_role": instance,
        "operator_role_arn": f"arn:aws:iam::{account}:role/reviewed-operator",
        "cluster": "example-cluster",
        "namespace": "adp-gateway",
        "registry_table": "example-registry",
        "internal_invoke_url": f"https://example123.execute-api.{region}.amazonaws.com/dev",
    }
    data = {
        "identity": {
            "Account": account,
            "Arn": f"arn:aws:sts::{account}:assumed-role/reviewed-operator/test",
        },
        "roles": roles,
        "policy": {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["execute-api:Invoke"],
                    "Resource": f"arn:aws:execute-api:{region}:{account}:example123/dev/POST/internal/v1/product-evaluation/retire",
                }
            ]
        },
        "recovery_policy": {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["execute-api:Invoke"],
                    "Resource": f"arn:aws:execute-api:{region}:{account}:example123/dev/POST/internal/v1/product-evaluation/retire",
                }
            ]
        },
        "entry": {
            "principalArn": roles[orchestrator]["Arn"],
            "kubernetesGroups": ["adp:cli-uplift-gateway-observer"],
        },
        "policies": [
            {
                "policyArn": VIEW,
                "accessScope": {"type": "namespace", "namespaces": ["adp-gateway"]},
            }
        ],
        "item": {
            "role_arn": {"S": roles[instance]["Arn"]},
            "iam_role_id": {"S": roles[instance]["RoleId"]},
            "credential_scopes": {"SS": ["internal:product-fixture:retire"]},
            "owner": {"S": "product-evaluator-legacy-v1"},
            "status": {"S": "active"},
        },
        "recovery_item": {
            "role_arn": {"S": roles[orchestrator]["Arn"]},
            "iam_role_id": {"S": roles[orchestrator]["RoleId"]},
            "credential_scopes": {"SS": ["internal:product-fixture:retire"]},
            "owner": {"S": "product-evaluator-legacy-v1"},
            "status": {"S": "active"},
        },
    }
    return config, data


def aws_for(data):
    class ReadOnlyPort:
        def __init__(self):
            self.calls = []

        def call(self, service, operation, **kwargs):
            self.calls.append((service, operation, kwargs))
            if (service, operation) == ("sts", "get_caller_identity"):
                return data["identity"]
            if (service, operation) == ("iam", "get_role"):
                return {"Role": data["roles"][kwargs["RoleName"]]}
            if (service, operation) == ("iam", "get_role_policy"):
                return {
                    "PolicyDocument": data[
                        "recovery_policy"
                        if kwargs["RoleName"] == "adp-cli-uplift-eval-orchestrator"
                        else "policy"
                    ]
                }
            if (service, operation) == ("eks", "describe_access_entry"):
                return {"accessEntry": data["entry"]}
            if (service, operation) == ("eks", "list_associated_access_policies"):
                return {
                    "associatedAccessPolicies": data["policies"],
                    **({"nextToken": "more"} if data.get("nextToken") else {}),
                }
            if (service, operation) == ("dynamodb", "get_item"):
                return {
                    "Item": data[
                        "recovery_item"
                        if kwargs["Key"]["agent_id"]["S"] == RECOVERY_REGISTRY_ID
                        else "item"
                    ]
                }
            pytest.fail(f"Unexpected AWS operation: {service}.{operation}")

    return ReadOnlyPort()


def test_installed_evaluator_reads_only_bounded_metadata(installed):
    config, data = installed
    aws = aws_for(data)
    verify(aws, config)
    assert [(service, operation) for service, operation, _ in aws.calls] == [
        ("sts", "get_caller_identity"),
        ("iam", "get_role"),
        ("iam", "get_role"),
        ("iam", "get_role_policy"),
        ("iam", "get_role_policy"),
        ("eks", "describe_access_entry"),
        ("eks", "list_associated_access_policies"),
        ("dynamodb", "get_item"),
        ("dynamodb", "get_item"),
    ]
    assert aws.calls[-1][2]["ConsistentRead"] is True
    assert aws.calls[-2][2]["Key"] == {"agent_id": {"S": REGISTRY_ID}}
    assert aws.calls[-1][2]["Key"] == {"agent_id": {"S": RECOVERY_REGISTRY_ID}}


@pytest.mark.parametrize(
    "break_item,reason",
    [
        ("wrong_role", "account or role"),
        ("wrong_tags", "ownership tags"),
        ("missing_retire", "cleanup invoke"),
        ("missing_recovery_invoke", "recovery invoke"),
        ("foreign_recovery_invoke", "recovery invoke"),
        ("missing_view", "inventory grant"),
        ("partial_view", "EKS policy inventory"),
        ("cluster_view", "inventory grant"),
        ("missing_registry", "registration"),
        ("foreign_registry", "registration"),
        ("missing_recovery_registry", "recovery registration"),
        ("foreign_recovery_registry", "recovery registration"),
    ],
)
def test_installed_mismatch_refuses_before_any_mutation(installed, break_item, reason):
    config, original = installed
    data = deepcopy(original)
    if break_item == "wrong_role":
        data["identity"]["Arn"] = "arn:aws:sts::000000000101:assumed-role/other/test"
    elif break_item == "wrong_tags":
        data["roles"][config["instance_role"]]["Tags"] = []
    elif break_item == "missing_retire":
        data["policy"]["Statement"] = []
    elif break_item == "missing_recovery_invoke":
        data["recovery_policy"]["Statement"] = []
    elif break_item == "foreign_recovery_invoke":
        data["recovery_policy"]["Statement"][0]["Resource"] = "foreign-target"
    elif break_item == "partial_view":
        data["nextToken"] = "more"
    elif break_item == "missing_view":
        data["policies"] = []
    elif break_item == "cluster_view":
        data["policies"][0]["accessScope"] = {"type": "cluster", "namespaces": []}
    elif break_item == "missing_registry":
        data["item"] = {}
    elif break_item == "missing_recovery_registry":
        data["recovery_item"] = {}
    elif break_item == "foreign_recovery_registry":
        data["recovery_item"]["iam_role_id"] = {"S": "foreign-role"}
    else:
        data["item"]["iam_role_id"] = {"S": "foreign-role"}
    aws = aws_for(data)
    with pytest.raises(ValueError, match=reason):
        verify(aws, config)
    assert all(
        operation.startswith(("get_", "list_", "describe_"))
        for _, operation, _ in aws.calls
    )
    if break_item == "wrong_role":
        assert len(aws.calls) == 1
