"""Tests for the routing-capable CFN role template (Issue #4742, child G of #4692).

The template is the security boundary for every routed Bedrock call, so these
tests assert its shape directly rather than trusting review to catch a drift.

Coverage:
  - v2 trust policy KEEPS sts:ExternalId + aws:PrincipalArn
  - v2 trust policy DROPS the aws:RequestTag/adp:user_id single-user pin
  - v2 grants bedrock:InvokeModel + ...WithResponseStream, resource-scoped
  - v2 does NOT attach ReadOnlyAccess and never uses Resource: "*"
  - v1 is byte-identical to main (widening it in place is explicitly rejected)
  - the launch-URL builder selects the right key and parameter set per version
"""

from __future__ import annotations

import os
import subprocess
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
    return [s for p in policies for s in p["PolicyDocument"]["Statement"]]


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

    def test_grants_no_non_bedrock_actions(self, v2):
        actions = {a for s in _inline_statements(v2) for a in _as_list(s["Action"])}
        assert actions, "v2 must grant something"
        non_bedrock = {a for a in actions if not a.startswith("bedrock:")}
        assert not non_bedrock, f"v2 should grant only Bedrock actions, found {non_bedrock}"

    def test_declares_an_output_role_arn(self, v2):
        """connect flow reads the role ARN back from stack outputs."""
        assert "RoleArn" in v2["Outputs"]


# ---------------------------------------------------------------------------
# v1 must not change
# ---------------------------------------------------------------------------


class TestV1Unchanged:
    def test_v1_still_pins_the_single_user(self):
        """v1's pin is a deliberate security property of its read-only
        agent-delegation purpose (§5.0 impl 4). This test fails if someone
        "fixes" v1 instead of adding v2."""
        v1 = _load(V1_PATH)
        for stmt in _trust_statements(v1):
            conditions = stmt["Condition"]["StringEquals"]
            assert conditions[USER_ID_PIN_CONDITION_KEY] == "UserSessionTag"

    def test_v1_still_read_only_and_grants_no_bedrock(self):
        v1 = _load(V1_PATH)
        assert _role(v1)["ManagedPolicyArns"] == ["arn:aws:iam::aws:policy/ReadOnlyAccess"]
        assert "Policies" not in _role(v1)

    def test_v1_is_byte_identical_to_main(self):
        """Issue validation criterion: v1 byte-identical to main. Widening it in
        place is explicitly rejected, so any diff at all is a failure."""
        repo_root = Path(__file__).resolve().parents[3]
        rel = V1_PATH.relative_to(repo_root)
        result = subprocess.run(
            ["git", "diff", "--exit-code", "origin/main", "--", str(rel)],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if result.returncode == 128:
            pytest.skip("origin/main not available in this checkout")
        assert result.returncode == 0, f"aws_role_v1.yaml differs from main:\n{result.stdout}"


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
