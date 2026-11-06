"""Gateway/recovery Identity grants and external secrets are deployment-scoped."""

import yaml

from tests.infra.test_gateway_role_iam_budget import DEV, WORST, consolidated_documents


def test_mcp_iam_scopes_only_isolated_provider_and_secrets():
    for target in (DEV, WORST):
        environment, region, account = target
        policies = consolidated_documents(*target, everything_on=target is WORST)
        statements = {row["Sid"]: row for document in policies.values() for row in document["Statement"]}
        prefix = f"arn:aws:bedrock-agentcore:{region}:{account}:"
        workload = f"{prefix}workload-identity-directory/default/workload-identity/adp-tools-{environment}-atlassian-mcp-oauth-user"
        provider = f"{prefix}token-vault/default/oauth2credentialprovider/adp-tools-{environment}-atlassian-mcp-user"
        assert statements["AtlassianMcpUserTokenAccess"] == {
            "Sid": "AtlassianMcpUserTokenAccess",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                "bedrock-agentcore:GetResourceOauth2Token",
                "bedrock-agentcore:CompleteResourceTokenAuth",
            ],
            "Resource": [workload],
        }
        assert statements["AtlassianMcpWorkloadSetup"]["Resource"] == [
            f"{prefix}workload-identity-directory/default",
            workload,
        ]
        assert statements["AtlassianMcpProviderSetup"]["Resource"] == [
            f"{prefix}token-vault/default",
            provider,
        ]
        assert set(statements["AtlassianMcpProviderSetup"]["Action"]) == {
            "bedrock-agentcore:GetOauth2CredentialProvider",
            "bedrock-agentcore:CreateOauth2CredentialProvider",
            "bedrock-agentcore:UpdateOauth2CredentialProvider",
            "bedrock-agentcore:ListTagsForResource",
            "bedrock-agentcore:TagResource",
        }
        secret_prefix = f"arn:aws:secretsmanager:{region}:{account}:secret:adp/{environment}/connections/atlassian-mcp"
        assert statements["AtlassianMcpPlanRead"] == {
            "Sid": "AtlassianMcpPlanRead",
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue"],
            "Resource": [f"{secret_prefix}/config-ABC123"],
        }
        assert statements["AtlassianMcpClientState"]["Resource"] == [f"{secret_prefix}/client-ABC123"]
        assert set(statements["AtlassianMcpClientState"]["Action"]) == {
            "secretsmanager:GetSecretValue",
            "secretsmanager:DescribeSecret",
            "secretsmanager:PutSecretValue",
        }
        for name in (
            "AtlassianMcpUserTokenAccess",
            "AtlassianMcpWorkloadSetup",
            "AtlassianMcpProviderSetup",
            "AtlassianMcpPlanRead",
            "AtlassianMcpClientState",
        ):
            assert "*" not in statements[name]["Resource"]
            assert not any("Delete" in action for action in statements[name]["Action"])
    from pathlib import Path

    gateway = Path(__file__).resolve().parents[2]
    pod = yaml.safe_load((gateway / "k8s/atlassian-mcp-recovery.yaml").read_text())
    assert pod["spec"]["jobTemplate"]["spec"]["template"]["spec"]["serviceAccountName"] == "gateway-service"
