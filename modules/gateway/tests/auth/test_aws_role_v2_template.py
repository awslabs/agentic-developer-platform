"""Tests for the routing-capable CFN role template (Issue #4742, child G of #4692).

The template is the security boundary for every routed Bedrock call, so these
tests assert its shape directly rather than trusting review to catch a drift.

Coverage:
  - v2 trust policy KEEPS sts:ExternalId + aws:PrincipalArn
  - v2 trust policy DROPS the aws:RequestTag/adp:user_id single-user pin
  - v2 grants bedrock:InvokeModel + ...WithResponseStream, resource-scoped
  - v2 does NOT attach ReadOnlyAccess and never uses Resource: "*"
  - v1 retains its user pin and AWS-managed broad-read permission contract
  - the launch-URL builder selects the right key and parameter set per version
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("ADP_CFN_TEMPLATE_BUCKET", "adp-test-cfn-templates")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "src" / "auth" / "cfn_templates"
V1_PATH = TEMPLATE_DIR / "aws_role_v1.yaml"
V2_PATH = TEMPLATE_DIR / "aws_role_v2.yaml"
REPO_ROOT = Path(__file__).resolve().parents[4]
CUSTOMER_GUIDE_PATH = REPO_ROOT / "docs" / "adp-platform-deployment" / "customer-aws-setup.md"
MANAGED_DEPLOY_GUIDE_PATH = REPO_ROOT / "docs" / "adp-platform-deployment" / "adp-managed-deploy.md"
DEPLOYMENT_EXAMPLE_PATH = REPO_ROOT / "config" / "deployment.yml.example"
DEPLOYMENT_INDEX_PATH = REPO_ROOT / "docs" / "adp-platform-deployment" / "README.md"
SELF_MANAGED_GUIDE_PATH = REPO_ROOT / "docs" / "adp-platform-deployment" / "self-managed-deploy.md"
ONBOARDING_GUIDE_PATH = REPO_ROOT / "docs" / "onboarding-walkthrough.md"
SETUP_ORG_SCRIPT_PATH = REPO_ROOT / "platform" / "scripts" / "setup-org.sh"
RETIRED_DEPLOY_TEMPLATE_PATH = REPO_ROOT / "modules" / "agent-factory" / "agent-worker-image" / "aws" / "deploy-write.cfn.yaml"
DEPLOY_CONTRACT_PATH = TEMPLATE_DIR / "aws_role_deploy_v1.yaml"

USER_ID_PIN_CONDITION_KEY = "aws:RequestTag/adp:user_id"


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation's short-form intrinsics.

    ``!Sub``/``!Ref``/``!GetAtt`` are unknown tags to SafeLoader. We only need the
    document structure here (which conditions and actions exist), not resolved
    values, so every intrinsic collapses to a plain string/list.
    """


def _intrinsic(loader, tag_suffix, node):  # pragma: no cover - trivial shim
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_CfnLoader.add_multi_constructor("!", _intrinsic)


def _load(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=_CfnLoader)


@pytest.fixture(scope="module")
def v2() -> dict:
    return _load(V2_PATH)


def _role(template: dict) -> dict:
    roles = [r for r in template["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    assert len(roles) == 1, "template should define exactly one IAM role"
    return roles[0]["Properties"]


def _trust_statements(template: dict) -> list[dict]:
    return _role(template)["AssumeRolePolicyDocument"]["Statement"]


# ---------------------------------------------------------------------------
# Trust policy — the §5.0b fix
# ---------------------------------------------------------------------------


class TestV2TrustPolicy:
    def test_template_is_parseable_and_has_a_role(self, v2):
        assert _role(v2)["RoleName"] == "ADP-Agent-${Nickname}"

    def test_every_statement_keeps_principal_arn_condition(self, v2):
        """aws:PrincipalArn pins the trust to ADP's gateway role specifically."""
        statements = _trust_statements(v2)
        assert statements, "trust policy must not be empty"
        for stmt in statements:
            conditions = stmt["Condition"]["StringEquals"]
            assert conditions["aws:PrincipalArn"] == "GatewayRolePrincipal"

    def test_assume_role_statement_keeps_external_id_condition(self, v2):
        """ExternalId is the load-bearing confused-deputy guard once the
        single-user tag condition is gone — it must not be dropped with it."""
        assume_stmts = [s for s in _trust_statements(v2) if "sts:AssumeRole" in _as_list(s["Action"])]
        assert assume_stmts, "trust policy must allow sts:AssumeRole"
        for stmt in assume_stmts:
            assert stmt["Condition"]["StringEquals"]["sts:ExternalId"] == "ExternalId"

    def test_no_statement_pins_a_single_user_id(self, v2):
        """The whole point of v2: no aws:RequestTag/adp:user_id condition.

        With it, the role is assumable on behalf of exactly one person and cannot
        serve a team or org mapping (design note §5.0b).
        """
        for stmt in _trust_statements(v2):
            for operator, conditions in stmt.get("Condition", {}).items():
                assert USER_ID_PIN_CONDITION_KEY not in conditions, f"v2 must not pin adp:user_id (found under {operator})"

    def test_user_session_tag_parameter_is_gone(self, v2):
        """A leftover parameter would be a CFN error at launch: build_launch_url
        omits param_UserSessionTag for v2."""
        assert "UserSessionTag" not in v2["Parameters"]

    def test_tag_session_still_permitted(self, v2):
        """Session tags are still SENT for CloudTrail attribution — they just
        stop being an authorization gate. So TagSession must still be allowed,
        or every tagged assume fails."""
        actions = [a for s in _trust_statements(v2) for a in _as_list(s["Action"])]
        assert "sts:TagSession" in actions

    def test_trust_is_scoped_to_the_platform_account(self, v2):
        for stmt in _trust_statements(v2):
            assert stmt["Principal"]["AWS"] == "arn:aws:iam::${GatewayAccountId}:root"
            assert stmt["Effect"] == "Allow"


# ---------------------------------------------------------------------------
# Permissions — the §5.0 fix
# ---------------------------------------------------------------------------


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def _inline_statements(template: dict) -> list[dict]:
    policies = _role(template).get("Policies", [])
    statements = []
    for policy in policies:
        for statement in policy["PolicyDocument"]["Statement"]:
            if isinstance(statement, dict):
                statements.append(statement)
                continue
            if isinstance(statement, list):
                statements.extend(item for item in statement if isinstance(item, dict))
    return statements


class TestV2Permissions:
    def test_grants_both_bedrock_invoke_actions(self, v2):
        """ReadOnlyAccess covers neither — they are mutating actions, which is
        why every routed call fails AccessDenied on a v1 role."""
        actions = {a for s in _inline_statements(v2) for a in _as_list(s["Action"])}
        assert "bedrock:InvokeModel" in actions
        assert "bedrock:InvokeModelWithResponseStream" in actions

    def test_does_not_attach_read_only_access(self, v2):
        """v2 exists to sign model calls, not to inspect the account. Granting
        ReadOnlyAccess here would hand routing destinations a privilege the
        customer never consented to for that purpose."""
        assert "ManagedPolicyArns" not in _role(v2)

    def test_never_uses_wildcard_resource(self, v2):
        """Repo IAM standard: resource-scoped, never Resource: "*"."""
        for stmt in _inline_statements(v2):
            for resource in _as_list(stmt["Resource"]):
                assert resource != "*", f"wildcard resource in statement {stmt.get('Sid')}"

    def test_bedrock_resources_are_scoped_to_model_arns(self, v2):
        resources = [r for s in _inline_statements(v2) for r in _as_list(s["Resource"])]
        assert any("foundation-model/" in r for r in resources)
        # Cross-region inference profiles (the us.anthropic.* ids the gateway
        # routes by default) resolve to inference-profile ARNs — omitting them
        # would make v2 fail for exactly the model ids most traffic uses.
        assert any(r.endswith("inference-profile/*") for r in resources)
        assert "arn:${AWS::Partition}:bedrock:*:${AWS::AccountId}:project/default" in resources

    def test_grants_no_non_bedrock_actions(self, v2):
        actions = {a for s in _inline_statements(v2) for a in _as_list(s["Action"])}
        assert actions, "v2 must grant something"
        non_bedrock = {a for a in actions if not a.startswith("bedrock:")}
        assert not non_bedrock, f"v2 should grant only Bedrock actions, found {non_bedrock}"

    def test_operation_inventory_is_exact(self, v2):
        """The steady-state customer role is not an infrastructure deploy role."""
        actions = {a for s in _inline_statements(v2) for a in _as_list(s["Action"])}
        assert actions == {"bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"}
        assert not any(action.startswith(("iam:", "sts:", "s3:", "kms:", "secretsmanager:")) for action in actions)

    def test_responses_api_uses_the_destination_default_project(self, v2):
        assert "EnableResponsesApi" not in v2["Parameters"]
        assert "Conditions" not in v2
        statement = next(item for item in _inline_statements(v2) if item["Sid"] == "InvokeBedrockResponses")
        assert statement["Action"] == "bedrock:InvokeModel"
        assert statement["Resource"] == "arn:${AWS::Partition}:bedrock:*:${AWS::AccountId}:project/default"

    def test_declares_an_output_role_arn(self, v2):
        """connect flow reads the role ARN back from stack outputs."""
        assert "RoleArn" in v2["Outputs"]


class TestV2ExternalIdContract:
    def test_accepts_the_uuid_generated_by_connect_start(self, v2):
        parameter = v2["Parameters"]["ExternalId"]
        generated = str(uuid.uuid4())
        assert parameter["MinLength"] <= len(generated) <= parameter["MaxLength"]
        assert re.fullmatch(parameter["AllowedPattern"], generated)

    def test_rejects_the_retired_64_character_contract(self, v2):
        parameter = v2["Parameters"]["ExternalId"]
        assert not re.fullmatch(parameter["AllowedPattern"], "a" * 64)


class TestCustomerRoleGuidance:
    def test_retires_the_admin_boundary_prototype_and_publishes_scoped_contract(self):
        assert not RETIRED_DEPLOY_TEMPLATE_PATH.exists()
        assert DEPLOY_CONTRACT_PATH.exists()
        for path in (CUSTOMER_GUIDE_PATH, MANAGED_DEPLOY_GUIDE_PATH, DEPLOYMENT_EXAMPLE_PATH):
            assert "deploy-write.cfn.yaml" not in path.read_text()

    def test_managed_bootstrap_is_explicitly_unsupported(self):
        customer_guide = CUSTOMER_GUIDE_PATH.read_text()
        managed_guide = MANAGED_DEPLOY_GUIDE_PATH.read_text()
        config_example = DEPLOYMENT_EXAMPLE_PATH.read_text()
        assert "publishes `aws_role_deploy_v1.yaml`" in customer_guide
        assert "Status: unavailable for cross-account customer bootstrap" in managed_guide
        assert "Cross-account bootstrap remains disabled" in config_example

    def test_setup_org_does_not_generate_the_retired_customer_account_example(self):
        setup_script = SETUP_ORG_SCRIPT_PATH.read_text()
        assert "Cross-account customer bootstrap is unavailable" in setup_script
        assert "Dashboard-linked roles are steady-state only" in setup_script
        assert "# Optional: cross-account deploy" not in setup_script
        assert "# customer_account:" not in setup_script

    def test_all_deployment_entry_points_mark_cross_account_bootstrap_unavailable(self):
        paths = (
            DEPLOYMENT_EXAMPLE_PATH,
            DEPLOYMENT_INDEX_PATH,
            SELF_MANAGED_GUIDE_PATH,
            ONBOARDING_GUIDE_PATH,
            SETUP_ORG_SCRIPT_PATH,
        )
        for path in paths:
            text = path.read_text().lower()
            assert "unavailable" in text, path
            assert "steady-state" in text, path

    def test_entry_points_do_not_advertise_linked_role_deployment(self):
        forbidden = (
            "customer_account block (this section)",
            "deploy into your account on your behalf",
            "deploys should leave it commented out",
            "unblocks every later phase for both tracks",
        )
        for path in (
            DEPLOYMENT_EXAMPLE_PATH,
            DEPLOYMENT_INDEX_PATH,
            SELF_MANAGED_GUIDE_PATH,
            ONBOARDING_GUIDE_PATH,
            SETUP_ORG_SCRIPT_PATH,
        ):
            text = path.read_text()
            assert not any(claim in text for claim in forbidden), path

    def test_guidance_never_instructs_customers_to_attach_admin(self):
        forbidden = ("Manually attach `AdministratorAccess`", "Attach `AdministratorAccess`", "install the deploy-capable role")
        for path in (CUSTOMER_GUIDE_PATH, MANAGED_DEPLOY_GUIDE_PATH, DEPLOYMENT_EXAMPLE_PATH, SETUP_ORG_SCRIPT_PATH):
            text = path.read_text()
            assert not any(instruction in text for instruction in forbidden), path


# ---------------------------------------------------------------------------
# v1 legacy broad-read contract
# ---------------------------------------------------------------------------


class TestV1LegacyContract:
    def test_v1_still_pins_the_single_user(self):
        """v1's pin is a deliberate security property of its read-only
        agent-delegation purpose (§5.0 impl 4). This test fails if someone
        "fixes" v1 instead of adding v2."""
        v1 = _load(V1_PATH)
        for stmt in _trust_statements(v1):
            conditions = stmt["Condition"]["StringEquals"]
            assert conditions[USER_ID_PIN_CONDITION_KEY] == "UserSessionTag"

    def test_v1_uses_aws_managed_broad_read_and_grants_no_bedrock(self):
        v1 = _load(V1_PATH)
        assert _role(v1)["ManagedPolicyArns"] == ["arn:aws:iam::aws:policy/ReadOnlyAccess"]
        assert "Policies" not in _role(v1)

    def test_guidance_discloses_data_reads_and_limits_least_privilege_claim(self):
        guide = CUSTOMER_GUIDE_PATH.read_text()
        assert "legacy personal inspection role attaches AWS-managed `ReadOnlyAccess`" in guide
        assert "including `s3:GetObject`" in guide
        assert "Only the Bedrock routing template is a least-privilege" in guide
        assert "do not describe it as least privilege" in guide


# ---------------------------------------------------------------------------
# Launch URL wiring
# ---------------------------------------------------------------------------


class TestLaunchUrlVersionSelection:
    """build_launch_url must send the parameter set the chosen template declares —
    CloudFormation hard-errors on an undeclared parameter."""

    @pytest.fixture(autouse=True)
    def _moto_s3(self):
        import boto3
        from moto import mock_aws

        with mock_aws():
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket=os.environ["ADP_CFN_TEMPLATE_BUCKET"])
            yield

    def _build(self, template_version: str | None) -> str:
        from src.auth.cfn_template import build_launch_url

        kwargs = {
            "credential_id": "cred-1",
            "nickname": "routing-dest",
            "external_id": "ext-123",
            "account_id": "123456789012",
            "user_id": "db-id-alice",
        }
        if template_version is not None:
            kwargs["template_version"] = template_version
        return build_launch_url(**kwargs)

    def test_v2_url_points_at_the_v2_template(self):
        from urllib.parse import unquote

        url = self._build("v2")
        assert "aws_role_v2.yaml" in unquote(url)

    def test_v2_url_omits_user_session_tag(self):
        """v2 declares no UserSessionTag parameter — sending it fails the stack."""
        assert "param_UserSessionTag" not in self._build("v2")

    def test_v2_url_still_sends_external_id_and_gateway_params(self):
        url = self._build("v2")
        assert "param_ExternalId=ext-123" in url
        assert "param_GatewayRolePrincipal=" in url
        assert "param_GatewayAccountId=" in url

    def test_default_is_v1_so_existing_flow_is_unchanged(self):
        from urllib.parse import unquote

        url = self._build(None)
        assert "aws_role_v1.yaml" in unquote(url)
        assert "param_UserSessionTag=db-id-alice" in url

    def test_explicit_v1_matches_the_default(self):
        from urllib.parse import unquote

        explicit = unquote(self._build("v1"))
        assert "aws_role_v1.yaml" in explicit
        assert "param_UserSessionTag=db-id-alice" in explicit
