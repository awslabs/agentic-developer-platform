"""gateway-role-policy-cutover.py decides the cutover phase from the live role and never mutates it.

Covers the operator flow in deployment-reference.md ("Gateway service role IAM
budget") with a fake IAM client: pass 1 captures every live legacy inline policy
byte-for-byte and refuses when the attachment quota cannot take the three
consolidated policies; pass 2 is reported once they are attached; a finished
environment is ``complete``. No AWS.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from tests.infra import hcl_policy_render as hcl

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "gateway-role-policy-cutover.py"
ROLE = "adp-dev-role-gateway-service"
GROUPS = ["adp-dev-policy-gateway-identity", "adp-dev-policy-gateway-data", "adp-dev-policy-gateway-agents"]
# The 18 attachments measured on dev (2026-10-16) minus the hand-created duplicate.
OTHER_ATTACHED = [
    "adp-dev-authority-probe-20260920",
    "adp-dev-deployment-evidence-read",
    "adp-dev-gateway-domain-operations",
    "adp-dev-policy-gateway-authorized-dispatch",
    "adp-dev-policy-gateway-chat-completion",
    "adp-dev-policy-gateway-chat-pending",
    "adp-dev-policy-gateway-chat-response",
    "adp-dev-policy-gateway-intake",
    "adp-dev-policy-gateway-task-api",
    "adp-dev-policy-gateway-task-source",
    "adp-dev-spp-1111-gateway-access",
    "adp-dev-spp-2222-gateway-access",
    "adp-dev-spp-3333-gateway-access",
    "adp-dev-spp-4444-gateway-access",
    "adp-dev-superplane-gateway-route-read",
    "adp-dev-superplane-provider-authority",
    "bedrockgw-dev-gateway-tools-catalogue-read",
]
DUPLICATE = "adp-dev-tools-catalogue-read"
COGNITO = "bedrockgw-dev-policy-gateway-cognito-lifecycle"
TOOLS = "bedrockgw-dev-gateway-tools-catalogue-read"


def document(actions, resource, sid=None):
    statement = {"Effect": "Allow", "Action": actions, "Resource": resource}
    if sid:
        statement = {"Sid": sid, **statement}
    return {"Version": "2012-10-17", "Statement": [statement]}


LIVE_INLINE = {
    "bedrockgw-dev-policy-gateway-cognito-read": document(
        ["cognito-idp:ListUsers"], "arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_abc", "CognitoReadUsers"
    ),
    "bedrockgw-dev-policy-gateway-vault-secrets": document(["secretsmanager:ListSecrets"], "*", "VaultSecretsList"),
    "bedrockgw-dev-gateway-user-oauth": document(
        ["bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
        ["arn:aws:bedrock-agentcore:us-east-1:123456789012:workload-identity-directory/default"],
        "UserScopedWorkloadToken",
    ),
    # Other stacks' inline grants on the same role are not this module's to retain.
    "adp-dev-policy-gateway-bedrock": document(["bedrock:InvokeModel"], "*"),
    "adp-dev-policy-gateway-agent-authority": document(["dynamodb:GetItem"], "*"),
}


def load():
    spec = importlib.util.spec_from_file_location("gateway_role_policy_cutover", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakePaginator:
    def __init__(self, key, items):
        self.key, self.items = key, items

    def paginate(self, **kwargs):
        assert kwargs == {"RoleName": ROLE}
        half = len(self.items) // 2
        yield {self.key: self.items[:half]}
        yield {self.key: self.items[half:]}


class FakeIam:
    def __init__(self, inline, attached):
        self.inline, self.attached = inline, attached
        self.calls = []

    def get_paginator(self, operation):
        if operation == "list_role_policies":
            return FakePaginator("PolicyNames", list(self.inline))
        if operation == "list_attached_role_policies":
            attached = [{"PolicyName": name, "PolicyArn": f"arn:aws:iam::123456789012:policy/{name}"} for name in self.attached]
            return FakePaginator("AttachedPolicies", attached)
        raise AssertionError(operation)

    def get_role_policy(self, **kwargs):
        assert kwargs["RoleName"] == ROLE
        name = kwargs["PolicyName"]
        self.calls.append(name)
        return {"RoleName": ROLE, "PolicyName": name, "PolicyDocument": self.inline[name]}

    def __getattr__(self, name):  # any mutating call is a test failure
        raise AssertionError(f"unexpected IAM call {name}")


def run(iam, tmp_path, capsys):
    module = load()
    output = tmp_path / "cutover.tfvars.json"
    code = module.main(["--environment", "dev", "--region", "us-east-1", "--output", str(output)], iam=iam)
    captured = capsys.readouterr()
    return code, captured.out.strip(), captured.err, output


class TestPassOne:
    @pytest.mark.parametrize("attached", [[], [TOOLS], [COGNITO], [TOOLS, COGNITO]])
    def test_pass_one_preserves_exactly_the_live_legacy_attachments(self, attached, tmp_path, capsys):
        """Render the real carrier counts from the helper's output, including older attached Cognito installs."""
        code, phase, _, output = run(FakeIam(LIVE_INLINE, attached), tmp_path, capsys)
        assert (code, phase) == (0, "cutover")
        tfvars = json.loads(output.read_text())
        assert tfvars["gateway_role_policy_cutover_managed_attachments"] == sorted(attached)
        source = hcl.blocks(ROOT / "infra" / "gateway-role-policies.tf")
        scope = hcl.Scope(
            {
                **{f"var.{key}": value for key, value in tfvars.items()},
                "local.name_prefix": "bedrockgw-dev",
                "local.gateway_service_irsa_role_name": ROLE,
            }
        )
        for suffix, name in (("tools_catalogue_read", TOOLS), ("cognito_lifecycle", COGNITO)):
            address = f"aws_iam_role_policy_attachment.gateway_{suffix}"
            carrier = next(block for block in source if block.address == address)
            count = hcl.evaluate(hcl.parse(carrier.attributes["count"]), scope)
            assert count == (1 if name in attached else 0), "retention must neither detach nor recreate a legacy attachment"
            assert hcl.evaluate(hcl.parse(carrier.attributes["role"]), scope) == ROLE
            assert carrier.attributes["policy_arn"] == f"aws_iam_policy.gateway_{suffix}[0].arn"
            assert any(block.kind == "moved" and block.attributes == {"from": address, "to": f"{address}[0]"} for block in source), (
                "an existing attachment must move to its carrier without deletion"
            )
            managed = next(block for block in source if block.address == f"aws_iam_policy.gateway_{suffix}")
            assert "ignore_changes = [policy]" in managed.body, "live scopes must survive pass 1 for pass 2 to compare"

    def test_cognito_retention_uses_no_additional_slot_at_the_quota(self, tmp_path, capsys):
        # Seventeen attachments including Cognito + three consolidated policies = twenty.
        attached = OTHER_ATTACHED[1:] + [COGNITO]
        code, phase, _, output = run(FakeIam(LIVE_INLINE, attached), tmp_path, capsys)
        assert (code, phase) == (0, "cutover")
        assert json.loads(output.read_text())["gateway_role_policy_cutover_managed_attachments"] == sorted([TOOLS, COGNITO])

    def test_cognito_is_counted_when_pass_one_would_exceed_the_quota(self, tmp_path, capsys):
        code, phase, err, output = run(FakeIam(LIVE_INLINE, OTHER_ATTACHED + [COGNITO]), tmp_path, capsys)
        assert (code, phase) == (2, "")
        assert "18 attached managed policies" in err
        assert not output.exists()

    def test_writes_every_live_legacy_document_unchanged_and_reports_cutover(self, tmp_path, capsys):
        iam = FakeIam(LIVE_INLINE, OTHER_ATTACHED)
        code, phase, err, output = run(iam, tmp_path, capsys)
        assert (code, phase) == (0, "cutover")
        tfvars = json.loads(output.read_text())
        assert tfvars["gateway_role_policy_cutover_keep_legacy"] is True
        documents = tfvars["gateway_role_policy_cutover_inline_documents"]
        assert sorted(documents) == ["cognito-read", "user-oauth", "vault-secrets"]
        for suffix, name in (("cognito-read", "bedrockgw-dev-policy-gateway-cognito-read"), ("user-oauth", "bedrockgw-dev-gateway-user-oauth")):
            assert json.loads(documents[suffix]) == LIVE_INLINE[name]
        assert sorted(iam.calls) == sorted(name for name in LIVE_INLINE if name.startswith("bedrockgw-"))
        assert "retains 3 inline policies and attaches 3 managed policies" in err

    def test_refuses_when_the_quota_cannot_take_the_three_attachments(self, tmp_path, capsys):
        iam = FakeIam(LIVE_INLINE, OTHER_ATTACHED + [DUPLICATE])
        code, phase, err, output = run(iam, tmp_path, capsys)
        assert code == 2
        assert phase == ""
        assert not output.exists(), "pass 1 must not be prepared when it would fail on the attachment quota"
        assert "18 attached managed policies" in err and "quota is 20" in err
        assert DUPLICATE in err and f"  - {DUPLICATE}" in err

    def test_unknown_bedrockgw_names_are_left_alone(self, tmp_path, capsys):
        inline = dict(LIVE_INLINE)
        inline["bedrockgw-dev-policy-gateway-something-new"] = {"Version": "2012-10-17", "Statement": []}
        iam = FakeIam(inline, OTHER_ATTACHED)
        code, phase, _, output = run(iam, tmp_path, capsys)
        assert (code, phase) == (0, "cutover")
        assert "something-new" not in json.loads(output.read_text())["gateway_role_policy_cutover_inline_documents"]

    def test_invalid_live_document_aborts(self, tmp_path, capsys):
        inline = dict(LIVE_INLINE)
        inline["bedrockgw-dev-policy-gateway-cognito-read"] = {"Version": "2008-10-17", "Statement": []}
        with pytest.raises(SystemExit, match="Invalid live gateway inline policy"):
            run(FakeIam(inline, OTHER_ATTACHED), tmp_path, capsys)


class TestPassTwoAndCompletion:
    def test_retire_is_reported_when_only_cognito_remains(self, tmp_path, capsys):
        code, phase, err, output = run(FakeIam({}, GROUPS + [COGNITO]), tmp_path, capsys)
        assert (code, phase) == (0, "retire")
        assert "0 legacy inline and 1 retired managed policies remain" in err
        assert not output.exists()

    def test_reports_retire_once_attached_while_legacy_grants_remain(self, tmp_path, capsys):
        iam = FakeIam(LIVE_INLINE, OTHER_ATTACHED + GROUPS)
        code, phase, err, output = run(iam, tmp_path, capsys)
        assert (code, phase) == (0, "retire")
        assert not output.exists()
        assert "3 legacy inline and 1 retired managed policies remain" in err

    def test_retire_is_reported_for_the_retired_managed_policy_alone(self, tmp_path, capsys):
        inline = {name: doc for name, doc in LIVE_INLINE.items() if not name.startswith("bedrockgw-")}
        code, phase, _, _ = run(FakeIam(inline, OTHER_ATTACHED + GROUPS), tmp_path, capsys)
        assert (code, phase) == (0, "retire")

    def test_complete_when_only_other_stacks_inline_grants_remain(self, tmp_path, capsys):
        inline = {name: doc for name, doc in LIVE_INLINE.items() if not name.startswith("bedrockgw-")}
        attached = [name for name in OTHER_ATTACHED if name != "bedrockgw-dev-gateway-tools-catalogue-read"] + GROUPS
        code, phase, err, output = run(FakeIam(inline, attached), tmp_path, capsys)
        assert (code, phase, err) == (0, "complete", "")
        assert not output.exists()


def test_legacy_suffixes_match_the_terraform_name_map():
    module = load()
    tf = (ROOT / "infra" / "gateway-role-policies.tf").read_text()
    block = tf[tf.index("gateway_role_legacy_inline_names = {") :]
    block = block[: block.index("\n  }")]
    declared = set()
    for line in block.splitlines()[1:]:
        declared.add(line.split("=")[0].strip())
    assert declared == module.LEGACY_SUFFIXES


@pytest.mark.parametrize("keep_legacy", [False, True])
def test_defaults_never_recreate_absent_legacy_attachments(keep_legacy):
    source = hcl.blocks(ROOT / "infra" / "gateway-role-policies.tf")
    scope = hcl.Scope(
        {
            f"var.{block.labels[0]}": hcl.evaluate(hcl.parse(block.attributes["default"]), hcl.Scope({}))
            for block in source
            if block.kind == "variable"
        }
    )
    scope.values.update({"local.name_prefix": "bedrockgw-dev", "var.gateway_role_policy_cutover_keep_legacy": keep_legacy})
    for block in source:
        if block.address in (
            "aws_iam_role_policy_attachment.gateway_cognito_lifecycle",
            "aws_iam_role_policy_attachment.gateway_tools_catalogue_read",
        ):
            assert hcl.evaluate(hcl.parse(block.attributes["count"]), scope) == 0


def test_pass_two_retires_carriers_even_if_the_attachment_inventory_is_left_set():
    source = hcl.blocks(ROOT / "infra" / "gateway-role-policies.tf")
    scope = hcl.Scope(
        {
            "var.gateway_role_policy_cutover_keep_legacy": False,
            "var.gateway_role_policy_cutover_managed_attachments": [TOOLS, COGNITO],
            "local.name_prefix": "bedrockgw-dev",
        }
    )
    for block in source:
        if block.address in (
            "aws_iam_role_policy_attachment.gateway_cognito_lifecycle",
            "aws_iam_role_policy_attachment.gateway_tools_catalogue_read",
        ):
            assert hcl.evaluate(hcl.parse(block.attributes["count"]), scope) == 0
