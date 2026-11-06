"""Retire frozen OAuth grants while keeping #8822's exact effective authority."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from tests.infra.test_gateway_role_iam_budget import DEV, WORST, consolidated_documents

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("oauth_upgrade_policy", ROOT / "platform/scripts/upgrade-plan-policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
ADDRESS = 'aws_iam_role_policy.gateway_role_legacy["user-oauth"]'
TARGETS = [DEV, WORST, ("prod", "eu-west-1", "123456789012")]


def legacy_oauth_statements(target):
    """Pre-consolidation declarations; kept independent of today's renderer."""
    environment, region, account = target
    prefix = f"arn:aws:bedrock-agentcore:{region}:{account}:"
    workload_resources = [
        f"{prefix}workload-identity-directory/default",
        f"{prefix}workload-identity-directory/default/workload-identity/adp-tools-{environment}-oauth-user",
    ]
    return [
        {
            "Sid": "UserScopedWorkloadToken",
            "Effect": "Allow",
            "Action": ["bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
            "Resource": workload_resources,
        },
        {
            "Sid": "UserOauthConsentAndToken",
            "Effect": "Allow",
            "Action": ["bedrock-agentcore:GetResourceOauth2Token", "bedrock-agentcore:CompleteResourceTokenAuth"],
            "Resource": workload_resources
            + [
                f"{prefix}token-vault/default",
                f"{prefix}token-vault/default/oauth2credentialprovider/adp-tools-{environment}-*-user",
            ],
        },
    ]


def retirement_plan(target):
    environment, _, account = target
    role = f"adp-{environment}-role-gateway-service"
    name = f"bedrockgw-{environment}-gateway-user-oauth"
    replacement_name = f"adp-{environment}-policy-gateway-identity"
    replacement_arn = f"arn:aws:iam::{account}:policy/{replacement_name}"
    document = consolidated_documents(*target, everything_on=True)["identity"]
    managed = {"name": replacement_name, "arn": replacement_arn, "policy": json.dumps(document)}
    attached = {"role": role, "policy_arn": replacement_arn}
    return {
        "variables": {"environment": {"value": environment}},
        "resource_changes": [
            {
                "address": ADDRESS,
                "type": "aws_iam_role_policy",
                "change": {
                    "actions": ["delete"],
                    "before": {
                        "name": name,
                        "role": role,
                        "id": f"{role}:{name}",
                        "policy": json.dumps({"Version": "2012-10-17", "Statement": legacy_oauth_statements(target)}),
                    },
                    "after": None,
                },
            },
            {
                "address": 'aws_iam_policy.gateway_role["identity"]',
                "type": "aws_iam_policy",
                "change": {"actions": ["no-op"], "before": managed, "after": copy.deepcopy(managed)},
            },
            {
                "address": 'aws_iam_role_policy_attachment.gateway_role["identity"]',
                "type": "aws_iam_role_policy_attachment",
                "change": {"actions": ["no-op"], "before": attached, "after": copy.deepcopy(attached)},
            },
        ],
    }


@pytest.mark.parametrize("target", TARGETS)
def test_rendered_identity_policy_passes_legacy_oauth_retirement(target):
    plan = retirement_plan(target)
    assert policy.evaluate(plan, "gateway", target[2]) == {"routine": [ADDRESS], "blocked": [], "protected": []}


@pytest.mark.parametrize("target", TARGETS)
def test_split_preserves_exact_per_action_resource_union(target):
    environment, region, account = target
    legacy = legacy_oauth_statements(target)
    atlassian = (
        f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default/"
        f"workload-identity/adp-tools-{environment}-atlassian-mcp-oauth-user"
    )
    # #8822 appended the same identity to both existing statements. Splitting
    # it out must preserve every action/resource pair, including wildcards.
    previously_authorized = copy.deepcopy(legacy)
    for statement in previously_authorized:
        statement["Resource"].append(atlassian)
    rendered = consolidated_documents(*target, everything_on=True)["identity"]["Statement"]
    by_sid = {statement["Sid"]: statement for statement in rendered}
    for statement in legacy:
        assert by_sid[statement["Sid"]] == statement
    actions = {action for statement in legacy for action in statement["Action"]}

    def effective_scopes(statements):
        scopes = {action: set() for action in actions}
        for statement in statements:
            relevant = set(statement["Action"]) & actions
            if relevant:
                assert statement["Effect"] == "Allow"
                assert "Condition" not in statement
                resources = statement["Resource"]
                assert isinstance(resources, list)
                for action in relevant:
                    scopes[action].update(resources)
        return scopes

    assert effective_scopes(rendered) == effective_scopes(previously_authorized)


@pytest.mark.parametrize("mutation", ["missing_resource", "extra_resource", "missing_action", "changed_condition", "unattached"])
def test_retirement_still_requires_exact_legacy_scopes_and_attachment(mutation):
    plan = retirement_plan(DEV)
    managed = plan["resource_changes"][1]["change"]
    document = json.loads(managed["before"]["policy"])
    statement = next(item for item in document["Statement"] if item["Sid"] == "UserScopedWorkloadToken")
    if mutation == "missing_resource":
        statement["Resource"].pop()
    elif mutation == "extra_resource":
        statement["Resource"].append(statement["Resource"][-1] + "-unexpected")
    elif mutation == "missing_action":
        statement["Action"] = []
    elif mutation == "changed_condition":
        statement["Condition"] = {"StringEquals": {"aws:RequestedRegion": "us-east-1"}}
    elif mutation == "unattached":
        plan["resource_changes"][2]["change"]["actions"] = ["create"]
    for side in ("before", "after"):
        managed[side]["policy"] = json.dumps(document)
    assert policy.evaluate(plan, "gateway", DEV[2]) == {"routine": [], "blocked": [ADDRESS], "protected": []}
