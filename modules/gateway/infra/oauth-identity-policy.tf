# User-scoped AgentCore OAuth (workload identity + token vault). Granted to the
# gateway service role through the consolidated identity policy in
# gateway-role-policies.tf (aws_iam_policy.gateway_role["identity"]).
locals {
  gateway_user_oauth_statements = [
    {
      Sid    = "UserScopedWorkloadToken"
      Effect = "Allow"
      Action = ["bedrock-agentcore:GetWorkloadAccessTokenForUserId"]
      Resource = [
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default",
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default/workload-identity/adp-tools-${var.environment}-oauth-user"
      ]
    },
    {
      Sid    = "UserOauthConsentAndToken"
      Effect = "Allow"
      Action = ["bedrock-agentcore:GetResourceOauth2Token", "bedrock-agentcore:CompleteResourceTokenAuth"]
      Resource = [
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default",
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default/workload-identity/adp-tools-${var.environment}-oauth-user",
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:token-vault/default",
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:token-vault/default/oauth2credentialprovider/adp-tools-${var.environment}-*-user"
      ]
    }
  ]
}

# Deployment-owned provider apps: the human platform-admin route can create
# only the three named apps and their dedicated external secrets. No provider
# update permission except for the GitHub provider: only an explicit admin
# replacement may rotate that provider and its dedicated secret.
locals {
  gateway_oauth_app_statements = [
    {
      Sid    = "OauthAppWorkloadSetup"
      Effect = "Allow"
      Action = ["bedrock-agentcore:GetWorkloadIdentity", "bedrock-agentcore:CreateWorkloadIdentity"]
      Resource = [
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default",
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default/workload-identity/adp-tools-${var.environment}-oauth-user"
      ]
    },
    {
      Sid    = "OauthAppRegistration"
      Effect = "Allow"
      Action = ["bedrock-agentcore:GetOauth2CredentialProvider", "bedrock-agentcore:CreateOauth2CredentialProvider"]
      Resource = concat([
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:token-vault/default"
        ], [for provider in ["github", "atlassian", "slack"] :
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:token-vault/default/oauth2credentialprovider/adp-tools-${var.environment}-${provider}-user"
      ])
    },
    {
      Sid      = "SlackAppSetupProgress"
      Effect   = "Allow"
      Action   = ["secretsmanager:PutSecretValue"]
      Resource = ["arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:adp/${var.environment}/connections/oauth/slack-??????"]
    },
    {
      Sid    = "OauthAppExternalSecrets"
      Effect = "Allow"
      Action = ["secretsmanager:CreateSecret", "secretsmanager:GetSecretValue"]
      Resource = [for provider in ["github", "atlassian", "slack"] :
        "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:adp/${var.environment}/connections/oauth/${provider}-??????"
      ]
    }
  ]
  gateway_oauth_app_replacement_statements = [
    {
      Sid      = "OauthAppGithubReplacement"
      Effect   = "Allow"
      Action   = ["bedrock-agentcore:UpdateOauth2CredentialProvider"]
      Resource = ["arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:token-vault/default/oauth2credentialprovider/adp-tools-${var.environment}-github-user"]
    },
    {
      Sid      = "GithubAppSecretRotation"
      Effect   = "Allow"
      Action   = ["secretsmanager:PutSecretValue"]
      Resource = ["arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:adp/${var.environment}/connections/oauth/github-??????"]
    }
  ]
  gateway_atlassian_mcp_statements = [
    {
      # AgentCore checks this dependent action when creating an OAuth provider,
      # including with an external client secret and an existing default vault.
      Sid      = "AtlassianMcpVaultBootstrap"
      Effect   = "Allow"
      Action   = ["bedrock-agentcore:CreateTokenVault"]
      Resource = ["arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:token-vault/default"]
    },
    {
      # Keep the original OAuth statements' resource lists unchanged so the
      # second-pass retirement gate can prove their legacy grants survived.
      # #8822 added this workload identity to both lists; the same three actions
      # on that identity live here with no change to effective authority.
      Sid    = "AtlassianMcpUserTokenAccess"
      Effect = "Allow"
      Action = [
        "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
        "bedrock-agentcore:GetResourceOauth2Token",
        "bedrock-agentcore:CompleteResourceTokenAuth"
      ]
      Resource = [
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default/workload-identity/adp-tools-${var.environment}-atlassian-mcp-oauth-user"
      ]
    },
    {
      Sid    = "AtlassianMcpWorkloadSetup"
      Effect = "Allow"
      Action = ["bedrock-agentcore:GetWorkloadIdentity", "bedrock-agentcore:CreateWorkloadIdentity"]
      Resource = [
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default",
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default/workload-identity/adp-tools-${var.environment}-atlassian-mcp-oauth-user"
      ]
    },
    {
      Sid    = "AtlassianMcpProviderSetup"
      Effect = "Allow"
      Action = [
        "bedrock-agentcore:GetOauth2CredentialProvider",
        "bedrock-agentcore:CreateOauth2CredentialProvider",
        "bedrock-agentcore:UpdateOauth2CredentialProvider",
        "bedrock-agentcore:ListTagsForResource",
        "bedrock-agentcore:TagResource"
      ]
      Resource = [
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:token-vault/default",
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:token-vault/default/oauth2credentialprovider/adp-tools-${var.environment}-atlassian-mcp-user"
      ]
    }
  ]
  gateway_atlassian_mcp_secret_statements = [
    {
      Sid      = "AtlassianMcpPlanRead"
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [aws_secretsmanager_secret.atlassian_mcp_config.arn]
    },
    {
      Sid      = "AtlassianMcpClientState"
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret", "secretsmanager:PutSecretValue"]
      Resource = [aws_secretsmanager_secret.atlassian_mcp_client.arn]
    }
  ]
}
