# =============================================================================
# GitLab CE Infrastructure — SSM Parameters for OIDC
# =============================================================================
# Stores Cognito OIDC client credentials in SSM Parameter Store so the GitLab
# instance can fetch them at boot time without baking secrets into user data.
# =============================================================================

resource "aws_ssm_parameter" "oidc_client_id" {
  name        = "/adp/${var.environment}/gitlab/oidc-client-id"
  description = "Cognito OIDC client ID for GitLab authentication"
  type        = "String"
  value       = aws_cognito_user_pool_client.gitlab_oidc.id

  tags = local.common_tags
}

resource "aws_ssm_parameter" "oidc_client_secret" {
  name        = "/adp/${var.environment}/gitlab/oidc-client-secret"
  description = "Cognito OIDC client secret for GitLab authentication"
  type        = "SecureString"
  value       = aws_cognito_user_pool_client.gitlab_oidc.client_secret

  tags = local.common_tags
}

resource "aws_ssm_parameter" "oidc_issuer" {
  name        = "/adp/${var.environment}/gitlab/oidc-issuer"
  description = "Cognito OIDC issuer URL for GitLab authentication"
  type        = "String"
  value       = "https://cognito-idp.${var.aws_region}.amazonaws.com/${var.cognito_user_pool_id}"

  tags = local.common_tags
}
