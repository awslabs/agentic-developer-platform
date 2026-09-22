from __future__ import annotations

import fnmatch
import json
from pathlib import Path

import pytest
import yaml
from botocore.session import Session

TEMPLATE = Path(__file__).resolve().parents[2] / "src" / "auth" / "cfn_templates" / "aws_role_deploy_v1.yaml"
DEPLOYMENT_ID = "customer-a24"
ACCOUNT_ID = "123456789012"


class _Loader(yaml.SafeLoader):
    pass


def _intrinsic(loader, _tag, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_Loader.add_multi_constructor("!", _intrinsic)


@pytest.fixture(scope="module")
def template() -> dict:
    return yaml.load(TEMPLATE.read_text(), Loader=_Loader)


@pytest.fixture(scope="module")
def role(template: dict) -> dict:
    return template["Resources"]["ADPBootstrapRole"]["Properties"]


@pytest.fixture(scope="module")
def statements(role: dict) -> list[dict]:
    found = []

    def visit(item):
        if isinstance(item, dict) and "Effect" in item:
            found.append(item)
        elif isinstance(item, dict | list):
            for child in item.values() if isinstance(item, dict) else item:
                visit(child)

    visit(role["Policies"][0]["PolicyDocument"]["Statement"])
    return found


def _statement(statements: list[dict], sid: str) -> dict:
    return next(item for item in statements if item["Sid"] == sid)


def _values(value) -> list[str]:
    return value if isinstance(value, list) else [value]


def _actions(statement: dict) -> set[str]:
    return set(_values(statement["Action"]))


def _render(value: str) -> str:
    return value.replace("${AWS::Partition}", "aws").replace("${AWS::AccountId}", ACCOUNT_ID).replace("${DeploymentId}", DEPLOYMENT_ID)


def _condition_matches(
    statement: dict,
    request_tags: dict[str, str],
    resource_tags: dict[str, str],
    requested_region: str,
    allowed_regions: list[str],
    ec2_create_action: str | None,
    tag_keys: list[str],
) -> bool:
    for key, expected in statement.get("Condition", {}).get("StringEquals", {}).items():
        expected = DEPLOYMENT_ID if expected == "DeploymentId" else expected
        if key.startswith("aws:RequestTag/"):
            actual = request_tags.get(key.removeprefix("aws:RequestTag/"))
        elif key.startswith("aws:ResourceTag/"):
            actual = resource_tags.get(key.removeprefix("aws:ResourceTag/"))
        elif key == "ec2:CreateAction":
            actual = ec2_create_action
        else:
            return False
        if actual not in _values(expected):
            return False
    for key, expected in statement.get("Condition", {}).get("ForAllValues:StringEquals", {}).items():
        if key != "aws:TagKeys" or not set(tag_keys).issubset(_values(expected)):
            return False
    for key, expected in statement.get("Condition", {}).get("StringNotEquals", {}).items():
        if key != "aws:RequestedRegion" or expected != "AllowedRegions":
            return False
        if requested_region in allowed_regions:
            return False
    return True


def _matches(
    statement: dict,
    action: str,
    resource: str,
    request_tags: dict[str, str],
    resource_tags: dict[str, str],
    requested_region: str,
    allowed_regions: list[str],
    ec2_create_action: str | None,
    tag_keys: list[str],
) -> bool:
    if "Action" in statement:
        action_matches = any(fnmatch.fnmatchcase(action.lower(), candidate.lower()) for candidate in _values(statement["Action"]))
    elif "NotAction" in statement:
        action_matches = not any(fnmatch.fnmatchcase(action.lower(), candidate.lower()) for candidate in _values(statement["NotAction"]))
    else:
        return False
    resource_matches = any(fnmatch.fnmatchcase(resource, _render(candidate)) for candidate in _values(statement["Resource"]))
    return (
        action_matches
        and resource_matches
        and _condition_matches(
            statement,
            request_tags,
            resource_tags,
            requested_region,
            allowed_regions,
            ec2_create_action,
            tag_keys,
        )
    )


def _decision(
    statements: list[dict],
    action: str,
    resource: str = "*",
    *,
    request_tags: dict[str, str] | None = None,
    resource_tags: dict[str, str] | None = None,
    requested_region: str = "us-east-1",
    allowed_regions: list[str] | None = None,
    ec2_create_action: str | None = None,
    tag_keys: list[str] | None = None,
) -> str:
    request_tags = request_tags or {}
    resource_tags = resource_tags or {}
    allowed_regions = allowed_regions or ["us-east-1"]
    tag_keys = tag_keys if tag_keys is not None else list(request_tags)
    matching = [
        item
        for item in statements
        if _matches(
            item,
            action,
            resource,
            request_tags,
            resource_tags,
            requested_region,
            allowed_regions,
            ec2_create_action,
            tag_keys,
        )
    ]
    if any(item["Effect"] == "Deny" for item in matching):
        return "explicitDeny"
    if any(item["Effect"] == "Allow" for item in matching):
        return "allowed"
    return "implicitDeny"


def test_template_is_ascii_and_never_attaches_administrator_access():
    assert TEMPLATE.read_bytes().isascii()
    assert "AdministratorAccess" not in TEMPLATE.read_text()


def test_trust_requires_external_id_and_exact_gateway_principal(role: dict):
    assume = next(item for item in role["AssumeRolePolicyDocument"]["Statement"] if item["Action"] == "sts:AssumeRole")
    assert assume["Condition"]["StringEquals"] == {
        "sts:ExternalId": "ExternalId",
        "aws:PrincipalArn": "GatewayRolePrincipal",
    }
    assert assume["Principal"]["AWS"].endswith(":${GatewayAccountId}:root")


def test_gateway_principal_account_must_match(template: dict):
    assertion = template["Rules"]["GatewayPrincipalBelongsToGatewayAccount"]["Assertions"][0]
    assert "GatewayAccountId" in str(assertion)
    assert "GatewayRolePrincipal" in str(assertion)


def test_optional_capabilities_are_explicit_and_default_off(template: dict):
    for parameter in (
        "EnableDataServices",
        "EnablePublicIngress",
        "EnableBedrockMarketplace",
        "EnableDestructiveTeardown",
    ):
        assert template["Parameters"][parameter]["Default"] == "false"
        assert template["Parameters"][parameter]["AllowedValues"] == ["true", "false"]
    assert "DataTeardownEnabled" in template["Conditions"]
    assert "PublicIngressTeardownEnabled" in template["Conditions"]
    assert "BedrockTeardownEnabled" in template["Conditions"]


def test_public_ingress_requires_cloudfront_certificate_region(template: dict):
    rule = template["Rules"]["PublicIngressIncludesCertificateRegion"]
    assert rule["RuleCondition"] == ["EnablePublicIngress", "true"]
    assert rule["Assertions"][0]["Assert"] == ["AllowedRegions", "us-east-1"]


def test_role_creation_and_arbitrary_trust_are_unconditionally_denied(statements: list[dict]):
    denied = _statement(statements, "DenyIdentityAndTrustChanges")
    assert "iam:*" in _actions(denied)
    allowed = set().union(*(_actions(item) for item in statements if item["Effect"] == "Allow"))
    assert not any(action.startswith("iam:") for action in allowed)
    for action in ("iam:CreateRole", "iam:UpdateAssumeRolePolicy", "iam:PassRole", "iam:CreateServiceLinkedRole"):
        assert _decision(statements, action, f"arn:aws:iam::{ACCOUNT_ID}:role/adp-{DEPLOYMENT_ID}-evil") == "explicitDeny"


def test_allow_inventory_contains_no_action_wildcards(statements: list[dict]):
    for statement in statements:
        if statement["Effect"] == "Allow":
            assert all("*" not in action for action in _actions(statement)), statement["Sid"]


@pytest.mark.parametrize(
    "action",
    [
        "ec2:StartInstances",
        "ec2:StopInstances",
        "lambda:InvokeFunction",
        "sns:Publish",
        "sqs:ReceiveMessage",
        "sqs:PurgeQueue",
    ],
)
def test_unrelated_customer_data_plane_operations_are_denied(statements: list[dict], action: str):
    assert _decision(statements, action, "*") == "explicitDeny"


def test_foundation_create_and_mutation_require_the_correct_ownership_tag(statements: list[dict]):
    assert _decision(statements, "ec2:CreateVpc", "*", request_tags={"adp:deployment-id": DEPLOYMENT_ID}) == "allowed"
    assert _decision(statements, "ec2:CreateVpc", "*") == "implicitDeny"
    assert _decision(statements, "ec2:CreateVpc", "*", request_tags={"adp:deployment-id": "another"}) == "implicitDeny"
    assert _decision(statements, "ec2:ModifyVpcAttribute", "*", resource_tags={"adp:deployment-id": DEPLOYMENT_ID}) == "allowed"
    assert _decision(statements, "ec2:ModifyVpcAttribute", "*", resource_tags={"adp:deployment-id": "another"}) == "implicitDeny"


def test_ec2_tag_on_create_dependent_permission_is_bounded(statements: list[dict]):
    ec2_create_actions = {
        action.removeprefix("ec2:") for action in _actions(_statement(statements, "CreateTaggedFoundationResources")) if action.startswith("ec2:")
    }
    dependent = _statement(statements, "TagFoundationResourcesOnCreate")
    conditions = dependent["Condition"]

    assert _actions(dependent) == {"ec2:CreateTags"}
    assert set(conditions["StringEquals"]["ec2:CreateAction"]) == ec2_create_actions
    assert conditions["StringEquals"]["aws:RequestTag/adp:deployment-id"] == "DeploymentId"
    assert conditions["ForAllValues:StringEquals"]["aws:TagKeys"] == ["adp:deployment-id"]

    request_tags = {"adp:deployment-id": DEPLOYMENT_ID}
    for create_action in ec2_create_actions:
        assert (
            _decision(
                statements,
                "ec2:CreateTags",
                request_tags=request_tags,
                ec2_create_action=create_action,
            )
            == "allowed"
        )

    assert _decision(statements, "ec2:CreateTags", request_tags=request_tags) == "implicitDeny"
    assert (
        _decision(
            statements,
            "ec2:CreateTags",
            request_tags=request_tags,
            ec2_create_action="RunInstances",
        )
        == "implicitDeny"
    )
    assert (
        _decision(
            statements,
            "ec2:CreateTags",
            request_tags={"adp:deployment-id": "another"},
            ec2_create_action="CreateVpc",
        )
        == "implicitDeny"
    )
    assert (
        _decision(
            statements,
            "ec2:CreateTags",
            request_tags=request_tags,
            ec2_create_action="CreateVpc",
            tag_keys=["adp:deployment-id", "Name"],
        )
        == "implicitDeny"
    )


def test_named_storage_and_repository_access_cannot_reach_unrelated_resources(statements: list[dict]):
    owned_bucket = f"arn:aws:s3:::adp-{DEPLOYMENT_ID}-state"
    other_bucket = "arn:aws:s3:::customer-production-data"
    assert _decision(statements, "s3:PutBucketVersioning", owned_bucket) == "allowed"
    assert _decision(statements, "s3:PutBucketVersioning", other_bucket) == "implicitDeny"
    owned_repo = f"arn:aws:ecr:us-east-1:{ACCOUNT_ID}:repository/adp-{DEPLOYMENT_ID}-gateway"
    other_repo = f"arn:aws:ecr:us-east-1:{ACCOUNT_ID}:repository/customer-app"
    assert _decision(statements, "ecr:PutImage", owned_repo) == "allowed"
    assert _decision(statements, "ecr:PutImage", other_repo) == "implicitDeny"


@pytest.mark.parametrize(
    ("action", "owned_resource"),
    [
        ("s3:PutBucketPolicy", f"arn:aws:s3:::adp-{DEPLOYMENT_ID}-state"),
        ("ecr:SetRepositoryPolicy", f"arn:aws:ecr:us-east-1:{ACCOUNT_ID}:repository/adp-{DEPLOYMENT_ID}-gateway"),
    ],
)
@pytest.mark.parametrize(
    "principal",
    [
        "arn:aws:iam::111122223333:role/adp-gateway",
        "arn:aws:iam::999900001111:role/unrelated",
    ],
)
def test_resource_policy_grants_cannot_be_created_for_any_principal(statements: list[dict], action: str, owned_resource: str, principal: str):
    grant_attempt = {
        "action": action,
        "resource": owned_resource,
        "policy": {"Statement": [{"Effect": "Allow", "Principal": {"AWS": principal}, "Action": "*"}]},
    }
    assert grant_attempt["policy"]["Statement"][0]["Principal"]["AWS"] == principal
    assert _decision(statements, grant_attempt["action"], grant_attempt["resource"]) == "implicitDeny"
    assert all(action not in _actions(item) for item in statements if item["Effect"] == "Allow")


def test_data_capability_uses_request_tags_only_for_create_and_resource_tags_afterward(statements: list[dict]):
    create = _statement(statements, "OptionalDataCreate")
    update = _statement(statements, "OptionalDataUpdate")
    teardown = _statement(statements, "OptionalDataTeardown")
    assert "aws:RequestTag/adp:deployment-id" in create["Condition"]["StringEquals"]
    for statement in (update, teardown):
        assert "aws:ResourceTag/adp:deployment-id" in statement["Condition"]["StringEquals"]
        assert "aws:RequestTag/adp:deployment-id" not in statement["Condition"]["StringEquals"]
    assert _decision(statements, "rds:CreateDBInstance", "*", request_tags={"adp:deployment-id": DEPLOYMENT_ID}) == "allowed"
    assert _decision(statements, "rds:ModifyDBInstance", "*", resource_tags={"adp:deployment-id": DEPLOYMENT_ID}) == "allowed"
    assert _decision(statements, "rds:DeleteDBInstance", "*", resource_tags={"adp:deployment-id": DEPLOYMENT_ID}) == "allowed"
    assert _decision(statements, "rds:DeleteDBInstance", "*", resource_tags={"adp:deployment-id": "another"}) == "implicitDeny"


def test_public_ingress_updates_and_teardown_are_resource_tag_scoped(statements: list[dict]):
    create_request = {
        "DistributionConfigWithTags": {
            "DistributionConfig": {},
            "Tags": {"Items": [{"Key": "adp:deployment-id", "Value": DEPLOYMENT_ID}]},
        }
    }
    operation = Session().get_service_model("cloudfront").operation_model("CreateDistributionWithTags")
    assert "Tags" in operation.input_shape.members["DistributionConfigWithTags"].members
    request_tags = {item["Key"]: item["Value"] for item in create_request["DistributionConfigWithTags"]["Tags"]["Items"]}
    assert (
        _decision(
            statements,
            "cloudfront:CreateDistribution",
            "*",
            request_tags=request_tags,
        )
        == "allowed"
    )
    assert _decision(statements, "cloudfront:CreateDistribution", "*") == "implicitDeny"
    assert "cloudfront:CreateDistributionWithTags" not in set().union(*(_actions(item) for item in statements if "Action" in item))
    for action in ("cloudfront:UpdateDistribution", "cloudfront:DeleteDistribution"):
        assert _decision(statements, action, "*", resource_tags={"adp:deployment-id": DEPLOYMENT_ID}) == "allowed"
        assert _decision(statements, action, "*", resource_tags={"adp:deployment-id": "another"}) == "implicitDeny"
    all_actions = set().union(*(_actions(item) for item in statements if "Action" in item))
    assert not any(action.startswith("route53:") for action in all_actions)


def test_teardown_is_scoped_to_named_or_tagged_resources(statements: list[dict]):
    owned = f"arn:aws:s3:::adp-{DEPLOYMENT_ID}-state"
    unrelated = "arn:aws:s3:::customer-production-data"
    assert _decision(statements, "s3:DeleteBucket", owned) == "allowed"
    assert _decision(statements, "s3:DeleteBucket", unrelated) == "implicitDeny"
    assert _decision(statements, "ec2:DeleteVpc", "*", resource_tags={"adp:deployment-id": DEPLOYMENT_ID}) == "allowed"
    assert _decision(statements, "ec2:DeleteVpc", "*", resource_tags={"adp:deployment-id": "another"}) == "implicitDeny"


def test_account_wide_optional_deletes_are_in_compound_teardown_capability(statements: list[dict]):
    normal = _statement(statements, "OptionalBedrockMarketplace")
    teardown = _statement(statements, "OptionalBedrockMarketplaceTeardown")
    assert "aws-marketplace:Unsubscribe" not in _actions(normal)
    assert "bedrock:DeleteModelInvocationLoggingConfiguration" not in _actions(normal)
    assert _actions(teardown) == {
        "aws-marketplace:Unsubscribe",
        "bedrock:DeleteModelInvocationLoggingConfiguration",
    }


def test_inline_policy_fits_aws_role_policy_limit(role: dict):
    document = role["Policies"][0]["PolicyDocument"]
    rendered = json.dumps(document, separators=(",", ":"))
    assert len(rendered) <= 10240


def test_bootstrap_and_steady_state_lifecycle_are_explicit(template: dict, role: dict):
    assert "ADPRuntimeBoundary" not in template["Resources"]
    assert "PermissionsBoundary" not in role
    tags = {item["Key"]: item["Value"] for item in role["Tags"]}
    assert tags["adp:access-phase"] == "bootstrap"
    assert "full ADP installation remains unsupported" in template["Outputs"]["SupportedScope"]["Value"]
    assert "prevent new sessions" in template["Outputs"]["Teardown"]["Value"]
    assert "expire within one hour" in template["Outputs"]["Teardown"]["Value"]


def test_region_boundary_preserves_global_services_and_denies_out_of_region_acm(statements: list[dict]):
    boundary = _statement(statements, "DenyRegionalActionsOutsideSelection")
    assert boundary["Condition"]["StringNotEquals"]["aws:RequestedRegion"] == "AllowedRegions"
    assert {"cloudfront:*", "iam:*"}.issubset(boundary["NotAction"])
    assert "acm:*" not in boundary["NotAction"]
    assert (
        _decision(
            statements,
            "acm:RequestCertificate",
            request_tags={"adp:deployment-id": DEPLOYMENT_ID},
            requested_region="eu-west-1",
            allowed_regions=["us-east-1", "us-west-2"],
        )
        == "explicitDeny"
    )
    assert (
        _decision(
            statements,
            "acm:RequestCertificate",
            request_tags={"adp:deployment-id": DEPLOYMENT_ID},
            requested_region="us-west-2",
            allowed_regions=["us-east-1", "us-west-2"],
        )
        == "allowed"
    )
