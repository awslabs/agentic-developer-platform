"""Dedicated setup observation roles never inherit gateway deployment powers."""

from pathlib import Path

from tests.infra import hcl_policy_render as hcl

POLICY = Path(__file__).resolve().parents[2] / "infra/atlassian-setup-observer.tf"


def test_opt_in_observer_roles_trust_only_protected_tools_environment():
    text = POLICY.read_text()
    assert "default     = false" in text
    assert "for_each = var.atlassian_mcp_setup_observer_roles_enabled" in text
    assert '"token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"' in text
    assert '"token.actions.githubusercontent.com:sub" = "repo:aws-e/adp:environment:adp-tools-deploy-${var.environment}"' in text
    assert "aws_iam_openid_connect_provider" not in text
    assert "trusted-deployment" not in text


def test_observer_policies_use_distinct_scoped_actions():
    blocks = hcl.blocks(POLICY)
    assert {block.labels for block in blocks if block.kind == "resource"} == {
        ("aws_iam_role", "atlassian_setup_observer"),
        ("aws_iam_role_policy", "atlassian_setup_observer"),
    }
    text = POLICY.read_text()
    assert 'each.key == "core"' in text
    assert '"secretsmanager:GetSecretValue"' in text
    assert '"bedrock-agentcore:GetResourceOauth2Token"' in text
    assert '"bedrock-agentcore:ListTagsForResource"' in text
    assert "PutSecretValue" not in text
    assert "CreateOauth2CredentialProvider" not in text
    assert "Delete" not in text
    assert 'Resource = "*"' not in text
