data "aws_secretsmanager_secret" "atlassian_mcp_client" {
  count = var.atlassian_mcp_identity_enabled ? 1 : 0
  name  = "adp/${var.environment}/connections/atlassian-mcp/client"
}

data "aws_kms_key" "atlassian_mcp_client" {
  for_each = toset(compact([for secret in data.aws_secretsmanager_secret.atlassian_mcp_client : secret.kms_key_id]))
  key_id   = each.value
}

resource "aws_iam_role_policy" "atlassian_mcp_identity" {
  count = var.atlassian_mcp_identity_enabled ? 1 : 0
  name  = "adp-tools-atlassian-mcp-identity"
  role  = aws_iam_role.runtime.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid    = "McpUserWorkloadToken"
        Effect = "Allow"
        Action = ["bedrock-agentcore:GetWorkloadAccessTokenForUserId"]
        Resource = [
          "arn:aws:bedrock-agentcore:${var.aws_region}:${var.aws_account_id}:workload-identity-directory/default",
          "arn:aws:bedrock-agentcore:${var.aws_region}:${var.aws_account_id}:workload-identity-directory/default/workload-identity/adp-tools-${var.environment}-atlassian-mcp-oauth-user"
        ]
      },
      {
        Sid    = "McpUserProviderToken"
        Effect = "Allow"
        Action = ["bedrock-agentcore:GetResourceOauth2Token"]
        Resource = [
          "arn:aws:bedrock-agentcore:${var.aws_region}:${var.aws_account_id}:workload-identity-directory/default",
          "arn:aws:bedrock-agentcore:${var.aws_region}:${var.aws_account_id}:workload-identity-directory/default/workload-identity/adp-tools-${var.environment}-atlassian-mcp-oauth-user",
          "arn:aws:bedrock-agentcore:${var.aws_region}:${var.aws_account_id}:token-vault/default",
          "arn:aws:bedrock-agentcore:${var.aws_region}:${var.aws_account_id}:token-vault/default/oauth2credentialprovider/adp-tools-${var.environment}-atlassian-mcp-user"
        ]
      },
      {
        # AgentCore refreshes the user's grant using this external OAuth client
        # secret under the caller's authority. No secret value enters Terraform.
        Sid      = "McpExternalClientSecret"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [for secret in data.aws_secretsmanager_secret.atlassian_mcp_client : secret.arn]
        Condition = {
          "ForAnyValue:StringEquals" = {
            "aws:CalledVia" = "bedrock-agentcore.amazonaws.com"
          }
        }
      }
      ], length(data.aws_kms_key.atlassian_mcp_client) == 0 ? [] : [{
        Sid      = "DecryptMcpExternalClientSecret"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = [for key in data.aws_kms_key.atlassian_mcp_client : key.arn]
        Condition = {
          StringEquals = {
            "kms:ViaService"                  = "secretsmanager.${var.aws_region}.amazonaws.com"
            "kms:EncryptionContext:SecretARN" = [for secret in data.aws_secretsmanager_secret.atlassian_mcp_client : secret.arn]
          }
        }
    }])
  })
}

# A shared Jira identity is distinct from the user-federated OAuth identity.
# These are AgentCore-managed keys; no external Secrets Manager secret is read.
resource "aws_iam_role_policy" "jira_system_identity" {
  count = var.atlassian_mcp_identity_enabled ? 1 : 0
  name  = "adp-tools-jira-system-identity"
  role  = aws_iam_role.runtime.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "JiraSystemWorkloadToken"
        Effect = "Allow"
        Action = ["bedrock-agentcore:GetWorkloadAccessToken"]
        Resource = [
          "arn:aws:bedrock-agentcore:${var.aws_region}:${var.aws_account_id}:workload-identity-directory/default",
          "arn:aws:bedrock-agentcore:${var.aws_region}:${var.aws_account_id}:workload-identity-directory/default/workload-identity/adp-tools-${var.environment}-atlassian-mcp-system"
        ]
      },
      {
        Sid    = "JiraSystemApiKey"
        Effect = "Allow"
        Action = ["bedrock-agentcore:GetResourceApiKey"]
        Resource = [
          "arn:aws:bedrock-agentcore:${var.aws_region}:${var.aws_account_id}:workload-identity-directory/default",
          "arn:aws:bedrock-agentcore:${var.aws_region}:${var.aws_account_id}:workload-identity-directory/default/workload-identity/adp-tools-${var.environment}-atlassian-mcp-system",
          "arn:aws:bedrock-agentcore:${var.aws_region}:${var.aws_account_id}:token-vault/default",
          "arn:aws:bedrock-agentcore:${var.aws_region}:${var.aws_account_id}:token-vault/default/apikeycredentialprovider/adp-tools-${var.environment}-atlassian-mcp-system-*"
        ]
      }
    ]
  })
}
