# =============================================================================
# GitHub OAuth Identity Provider (Issue #313)
# =============================================================================
# Adds GitHub as a federated OIDC identity provider to Cognito.
# GitHub does not implement full OIDC discovery, so endpoints are configured
# explicitly. This is gated behind var.enable_github_oauth.
# =============================================================================

# --- Secrets Manager: GitHub OAuth App credentials ----------------------------

resource "aws_secretsmanager_secret" "github_oauth" {
  count       = var.enable_github_oauth ? 1 : 0
  name        = "adp/${var.environment}/cognito/github-oauth-credentials"
  description = "GitHub OAuth App client_id and client_secret for Cognito federation"

  tags = merge(var.common_tags, {
    Name    = "adp-${var.environment}-github-oauth-credentials"
    Service = "secrets-manager"
    Purpose = "github-oauth-federation"
  })
}

resource "aws_secretsmanager_secret_version" "github_oauth" {
  count     = var.enable_github_oauth ? 1 : 0
  secret_id = aws_secretsmanager_secret.github_oauth[0].id
  secret_string = jsonencode({
    client_id     = var.github_oauth_client_id
    client_secret = var.github_oauth_client_secret
  })
}

# --- Cognito Identity Provider: GitHub (OIDC) --------------------------------

resource "aws_cognito_identity_provider" "github" {
  count         = var.enable_github_oauth ? 1 : 0
  user_pool_id  = aws_cognito_user_pool.main.id
  provider_name = "GitHub"
  provider_type = "OIDC"

  provider_details = {
    # GitHub OAuth endpoints (not standard OIDC discovery — configured explicitly)
    authorize_scopes              = "user:email read:org"
    client_id                     = var.github_oauth_client_id
    client_secret                 = var.github_oauth_client_secret
    oidc_issuer                   = "https://github.com"
    authorize_url                 = "https://github.com/login/oauth/authorize"
    token_url                     = "https://github.com/login/oauth/access_token"
    attributes_url                = "https://api.github.com/user"
    attributes_url_add_attributes = "true"
    # GitHub returns JSON from userinfo, use GET method
    attributes_request_method = "GET"
  }

  # Map GitHub user attributes to Cognito attributes
  attribute_mapping = {
    username = "sub"
    email    = "email"
    name     = "name"
    picture  = "avatar_url"
  }

  lifecycle {
    ignore_changes = [provider_details["client_secret"]]
  }
}
