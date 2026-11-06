"""Audit the installed legacy evaluator without mutating AWS or Kubernetes."""

import argparse
import json
import re
from pathlib import Path

from tests.e2e.cli_uplift.ports import AwsPort

REGISTRY_ID = "00000000-0000-0000-0000-000000008243"
RECOVERY_REGISTRY_ID = "00000000-0000-0000-0000-000008243001"
TAGS = {
    "Project": "adp",
    "Environment": "dev",
    "Purpose": "cli-uplift-eval",
    "Issue": "5248",
}
VIEW = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy"


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def verify(aws, config):
    account = config["account"]
    region = config["region"]
    identity = aws.call("sts", "get_caller_identity")
    role = config["operator_role_arn"]
    require(
        re.fullmatch(
            rf"arn:aws:iam::{re.escape(account)}:role/[A-Za-z0-9+=,.@_/-]+", role
        )
        and identity.get("Account") == account
        and identity.get("Arn", "").startswith(
            f"arn:aws:sts::{account}:assumed-role/{role.rsplit('/', 1)[-1]}/"
        ),
        "Operator account or role differs from the reviewed connection",
    )
    require(
        config["orchestrator_role"] == "adp-cli-uplift-eval-orchestrator"
        and config["instance_role"] == "adp-cli-uplift-eval-instance"
        and config["namespace"] == "adp-gateway",
        "Legacy evaluator target differs from the reviewed scope",
    )
    roles = {}
    for key in ("orchestrator_role", "instance_role"):
        name = config[key]
        record = aws.call("iam", "get_role", RoleName=name)["Role"]
        tags = {tag["Key"]: tag["Value"] for tag in record.get("Tags", [])}
        require(
            record.get("Arn") == f"arn:aws:iam::{account}:role/{name}"
            and isinstance(record.get("RoleId"), str)
            and bool(record["RoleId"])
            and all(
                tags.get(tag) == value
                for tag, value in (
                    TAGS if key == "instance_role" else {"Purpose": "cli-uplift-eval"}
                ).items()
            ),
            "Legacy evaluator role identity or ownership tags differ",
        )
        roles[key] = record
    origin = config["internal_invoke_url"]
    match = re.fullmatch(
        rf"https://([a-z0-9]+)\.execute-api\.{re.escape(region)}\.amazonaws\.com/([A-Za-z0-9_-]+)",
        origin,
    )
    require(match is not None, "Reviewed internal invoke URL is invalid")
    expected = (
        f"arn:aws:execute-api:{region}:{account}:{match[1]}/{match[2]}"
        "/POST/internal/v1/product-evaluation/retire"
    )
    policy = aws.call(
        "iam",
        "get_role_policy",
        RoleName=config["instance_role"],
        PolicyName="protected-product-fixture-retire",
    )["PolicyDocument"]
    require(
        policy.get("Statement")
        == [
            {
                "Effect": "Allow",
                "Action": ["execute-api:Invoke"],
                "Resource": expected,
            }
        ],
        "Protected cleanup invoke grant is absent or differs from the reviewed route",
    )
    recovery_policy = aws.call(
        "iam",
        "get_role_policy",
        RoleName=config["orchestrator_role"],
        PolicyName="protected-product-recovery-retire",
    )["PolicyDocument"]
    require(
        recovery_policy.get("Statement") == policy["Statement"],
        "Independent recovery invoke grant is absent or differs from the reviewed route",
    )
    entry = aws.call(
        "eks",
        "describe_access_entry",
        clusterName=config["cluster"],
        principalArn=roles["orchestrator_role"]["Arn"],
    )["accessEntry"]
    require(
        entry.get("principalArn") == roles["orchestrator_role"]["Arn"]
        and "adp:cli-uplift-gateway-observer" in entry.get("kubernetesGroups", []),
        "Retained EKS access entry or observer group differs",
    )
    association_page = aws.call(
        "eks",
        "list_associated_access_policies",
        clusterName=config["cluster"],
        principalArn=roles["orchestrator_role"]["Arn"],
    )
    require(not association_page.get("nextToken"), "EKS policy inventory is incomplete")
    policies = association_page["associatedAccessPolicies"]
    require(
        any(
            item.get("policyArn") == VIEW
            and item.get("accessScope")
            == {
                "type": "namespace",
                "namespaces": [config["namespace"]],
            }
            for item in policies
        )
        and not any(
            item.get("policyArn") == VIEW
            and item.get("accessScope")
            != {
                "type": "namespace",
                "namespaces": [config["namespace"]],
            }
            for item in policies
        ),
        "Namespace-scoped read-only pod inventory grant is absent or too broad",
    )
    item = aws.call(
        "dynamodb",
        "get_item",
        TableName=config["registry_table"],
        Key={"agent_id": {"S": REGISTRY_ID}},
        ConsistentRead=True,
    ).get("Item", {})
    require(
        item.get("role_arn") == {"S": roles["instance_role"]["Arn"]}
        and item.get("iam_role_id") == {"S": roles["instance_role"]["RoleId"]}
        and item.get("credential_scopes") == {"SS": ["internal:product-fixture:retire"]}
        and item.get("owner") == {"S": "product-evaluator-legacy-v1"}
        and item.get("status") == {"S": "active"},
        "Protected instance registration is missing or belongs to another role",
    )
    recovery = aws.call(
        "dynamodb",
        "get_item",
        TableName=config["registry_table"],
        Key={"agent_id": {"S": RECOVERY_REGISTRY_ID}},
        ConsistentRead=True,
    ).get("Item", {})
    require(
        recovery.get("role_arn") == {"S": roles["orchestrator_role"]["Arn"]}
        and recovery.get("iam_role_id") == {"S": roles["orchestrator_role"]["RoleId"]}
        and recovery.get("credential_scopes")
        == {"SS": ["internal:product-fixture:retire"]}
        and recovery.get("owner") == {"S": "product-evaluator-legacy-v1"}
        and recovery.get("status") == {"S": "active"},
        "Protected recovery registration is missing or belongs to another role",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    try:
        verify(AwsPort(config["region"]), config)
    except (KeyError, TypeError, ValueError, RuntimeError):
        raise SystemExit(
            "Legacy evaluator read-only preflight refused; check private operator evidence"
        ) from None
    print(
        "Legacy evaluator metadata preflight passed; per-pod runtime proof remains required"
    )


if __name__ == "__main__":
    main()
