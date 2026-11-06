variable "atlassian_mcp_setup_observer_roles_enabled" {
  type        = bool
  description = "Opt in to dedicated protected-workflow observer roles after reviewing the IAM plan."
  default     = false
}

locals {
  atlassian_setup_workload_arn = "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default/workload-identity/adp-tools-${var.environment}-atlassian-mcp-oauth-user"
  atlassian_setup_provider_arn = "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:token-vault/default/oauth2credentialprovider/adp-tools-${var.environment}-atlassian-mcp-user"
  atlassian_setup_directory    = "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default"
  atlassian_setup_vault        = "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:token-vault/default"
}

resource "aws_iam_role" "atlassian_setup_observer" {
  for_each = var.atlassian_mcp_setup_observer_roles_enabled ? toset(["core", "adapter"]) : toset([])

  name = "adp-${var.environment}-atlassian-mcp-observer-${each.key}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com" }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "repo:aws-e/adp:environment:adp-tools-deploy-${var.environment}"
      } }
    }]
  })
}

resource "aws_iam_role_policy" "atlassian_setup_observer" {
  for_each = aws_iam_role.atlassian_setup_observer

  name = "atlassian-setup-${each.key}-read"
  role = each.value.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = each.key == "core" ? [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [aws_secretsmanager_secret.atlassian_mcp_config.arn, aws_secretsmanager_secret.atlassian_mcp_client.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:GetOauth2CredentialProvider", "bedrock-agentcore:ListTagsForResource"]
        Resource = [local.atlassian_setup_provider_arn, local.atlassian_setup_vault]
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:GetWorkloadIdentity"]
        Resource = [local.atlassian_setup_directory, local.atlassian_setup_workload_arn]
      }
      ] : [
      {
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:GetWorkloadAccessTokenForUserId"]
        Resource = [local.atlassian_setup_directory, local.atlassian_setup_workload_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:GetResourceOauth2Token"]
        Resource = [local.atlassian_setup_directory, local.atlassian_setup_workload_arn, local.atlassian_setup_vault, local.atlassian_setup_provider_arn]
      }
    ]
  })
}

output "atlassian_setup_observer_role_arns" {
  description = "Dedicated read-only Identity and owner-token roles for protected Atlassian setup evaluation."
  value       = { for role, identity in aws_iam_role.atlassian_setup_observer : role => identity.arn }
}
