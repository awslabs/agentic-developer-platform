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

# =============================================================================
# GitLab Agent Integration — SSM Parameters (Issue #3436)
# =============================================================================
# Config params for the agent-worker GitLab acknowledge path (Tier A).
# The agent-worker ScaledJob reads GITLAB_URL from these; the API token is in
# Secrets Manager (below). Test project params are used by E2E tests.
# =============================================================================

resource "aws_ssm_parameter" "gitlab_url" {
  name        = "/adp/${var.environment}/gitlab/url"
  description = "GitLab instance base URL for agent-worker API calls"
  type        = "String"
  value       = "http://gitlab.${var.environment}.adp.internal"

  tags = local.common_tags
}

resource "aws_ssm_parameter" "gitlab_api_token_arn" {
  name        = "/adp/${var.environment}/gitlab/api-token-arn"
  description = "ARN of the Secrets Manager secret holding the GitLab group access token"
  type        = "String"
  value       = aws_secretsmanager_secret.gitlab_api_token.arn

  tags = local.common_tags
}

resource "aws_ssm_parameter" "gitlab_test_project_id" {
  name        = "/adp/${var.environment}/gitlab/test-project-id"
  description = "GitLab project ID for E2E integration tests"
  type        = "String"
  # Placeholder — seeded after GitLab instance setup (manual follow-up)
  value = "0"

  tags = local.common_tags

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "gitlab_test_project_path" {
  name        = "/adp/${var.environment}/gitlab/test-project-path"
  description = "GitLab project path for E2E integration tests (e.g. spike-group/test-project)"
  type        = "String"
  # Placeholder — seeded after GitLab instance setup (manual follow-up)
  value = "spike-group/test-project"

  tags = local.common_tags

  lifecycle {
    ignore_changes = [value]
  }
}

# =============================================================================
# GitLab API Token — Secrets Manager (Issue #3436)
# =============================================================================
# Shell secret for the GitLab group access token (api scope). The actual token
# is minted on the GitLab instance and written to this secret as a manual
# follow-up (ops step). The agent-worker reads this at runtime to post
# ack comments and create branches.
# =============================================================================

resource "aws_secretsmanager_secret" "gitlab_api_token" {
  name        = "adp/${var.environment}/gitlab-api-token"
  description = "GitLab group access token (api scope) for agent-worker Tier-A path. Minted on the GitLab instance; seeded manually."

  tags = local.common_tags
}

resource "aws_secretsmanager_secret_version" "gitlab_api_token_placeholder" {
  secret_id     = aws_secretsmanager_secret.gitlab_api_token.id
  secret_string = "PLACEHOLDER-seed-after-gitlab-instance-setup"

  lifecycle {
    ignore_changes = [secret_string]
  }
}
