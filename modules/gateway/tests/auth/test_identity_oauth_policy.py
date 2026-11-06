"""Keep the broker's Identity permissions on its service role, not agents."""

from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "infra" / "oauth-identity-policy.tf"
ROLE_POLICIES = Path(__file__).resolve().parents[2] / "infra" / "gateway-role-policies.tf"


@pytest.mark.parametrize("target", [("dev", "us-east-1", "000000000000"), ("prod", "eu-west-1", "123456789012")])
def test_provider_bootstrap_only_authorizes_the_deployments_default_vault(target):
    from tests.infra.test_gateway_role_iam_budget import consolidated_documents

    _, region, account = target
    documents = consolidated_documents(*target, everything_on=True)
    matching = [row for document in documents.values() for row in document["Statement"] if "bedrock-agentcore:CreateTokenVault" in row["Action"]]
    assert matching == [
        {
            "Sid": "AtlassianMcpVaultBootstrap",
            "Effect": "Allow",
            "Action": ["bedrock-agentcore:CreateTokenVault"],
            "Resource": [f"arn:aws:bedrock-agentcore:{region}:{account}:token-vault/default"],
        }
    ]


def test_identity_policy_limits_user_token_and_consent_to_gateway_service():
    policy = SOURCE.read_text()
    # The statements are a local consumed by the consolidated identity managed
    # policy, which is attached to the gateway service role (#7944).
    assert "gateway_user_oauth_statements = [" in policy
    role_policies = ROLE_POLICIES.read_text()
    start = role_policies.index("gateway_role_identity_statements = concat(")
    identity = role_policies[start : role_policies.index("gateway_role_data_statements = concat(")]
    assert "local.gateway_user_oauth_statements," in identity
    attachment = role_policies[role_policies.index('resource "aws_iam_role_policy_attachment" "gateway_role"') :]
    assert "role       = local.gateway_service_irsa_role_name" in attachment[: attachment.index("\n}\n")]
    assert "bedrock-agentcore:GetWorkloadAccessTokenForUserId" in policy
    assert "bedrock-agentcore:GetResourceOauth2Token" in policy
    assert "bedrock-agentcore:CompleteResourceTokenAuth" in policy
    assert "workload-identity/adp-tools-${var.environment}-oauth-user" in policy
    assert "oauth2credentialprovider/adp-tools-${var.environment}-*-user" in policy
    assert 'Resource = "*"' not in policy


def test_registration_allows_only_github_replacement_and_no_general_secret_access():
    from tests.infra.test_gateway_role_iam_budget import DEV, consolidated_documents

    documents = consolidated_documents(*DEV, everything_on=True)
    additions = {
        row["Sid"]: row
        for document in documents.values()
        for row in document["Statement"]
        if row["Sid"].startswith(("OauthApp", "GithubAppSecretRotation"))
    }
    assert set(additions) == {
        "OauthAppExternalSecrets",
        "OauthAppRegistration",
        "OauthAppWorkloadSetup",
        "OauthAppGithubReplacement",
        "GithubAppSecretRotation",
    }
    assert additions["OauthAppExternalSecrets"]["Action"] == ["secretsmanager:CreateSecret", "secretsmanager:GetSecretValue"]
    assert len(additions["OauthAppExternalSecrets"]["Resource"]) == 3
    for arn in additions["OauthAppExternalSecrets"]["Resource"]:
        assert ":connections/" not in arn
        assert "/connections/oauth/" in arn and arn.endswith("-??????")
    for row in additions.values():
        assert all(not any(word in action for word in ["Delete", "*"]) for action in row["Action"])
    assert additions["OauthAppGithubReplacement"]["Action"] == ["bedrock-agentcore:UpdateOauth2CredentialProvider"]
    assert additions["OauthAppGithubReplacement"]["Resource"][0].endswith("-github-user")
    assert additions["GithubAppSecretRotation"]["Action"] == ["secretsmanager:PutSecretValue"]
    assert additions["GithubAppSecretRotation"]["Resource"][0].endswith("/github-??????")
    progress = next(row for document in documents.values() for row in document["Statement"] if row["Sid"] == "SlackAppSetupProgress")
    assert progress["Action"] == ["secretsmanager:PutSecretValue"]
    assert len(progress["Resource"]) == 1
    assert progress["Resource"][0].endswith("secret:adp/dev/connections/oauth/slack-??????")
