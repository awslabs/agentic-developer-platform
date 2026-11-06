"""Render the service grant separately from gateway/adapter role permissions."""

import json
from pathlib import Path

import pytest

from tests.infra import hcl_policy_render as hcl

POLICY = Path(__file__).resolve().parents[2] / "infra/atlassian-mcp-secret-policy.tf"


@pytest.mark.parametrize(
    ("environment", "region", "account"),
    [("dev", "us-east-1", "111111111111"), ("staging", "ap-southeast-2", "222222222222")],
)
def test_identity_service_can_read_only_current_account_mcp_client_secret(environment, region, account):
    client_arn = f"arn:aws:secretsmanager:{region}:{account}:secret:adp/{environment}/connections/atlassian-mcp/client-AbCd12"
    scope = hcl.Scope(
        {
            "aws_secretsmanager_secret.atlassian_mcp_client.arn": client_arn,
            "data.aws_caller_identity.current.account_id": account,
        }
    )
    blocks = hcl.blocks(POLICY)
    assert len(blocks) == 1
    block = blocks[0]
    assert block.kind == "resource"
    assert block.labels == ("aws_secretsmanager_secret_policy", "atlassian_mcp_client")
    attrs = {key: hcl.evaluate(hcl.parse(value), scope) for key, value in block.attributes.items()}
    assert attrs["secret_arn"] == client_arn
    assert attrs["block_public_policy"] is True
    document = json.loads(attrs["policy"])
    assert document == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AtlassianMcpIdentityClientRead",
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "secretsmanager:GetSecretValue",
                "Resource": client_arn,
                "Condition": {"StringEquals": {"aws:SourceAccount": account}},
            }
        ],
    }
