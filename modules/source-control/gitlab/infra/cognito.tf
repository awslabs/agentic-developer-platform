# =============================================================================
# GitLab CE Infrastructure — Cognito OIDC Integration
# =============================================================================
# Creates a dedicated Cognito App Client for GitLab OIDC authentication.
# The user pool itself is owned by the gateway module; we reference it by ID.
# =============================================================================

# -----------------------------------------------------------------------------
# Data source: look up existing Cognito User Pool
# -----------------------------------------------------------------------------

data "aws_cognito_user_pools" "main" {
  name = "adp-${var.environment}"
}

# -----------------------------------------------------------------------------
# App Client for GitLab OIDC (server-side, with client secret)
# -----------------------------------------------------------------------------

resource "aws_cognito_user_pool_client" "gitlab_oidc" {
  name         = "${local.name_prefix}-oidc"
  user_pool_id = var.cognito_user_pool_id

  # Server-side OIDC flow requires a client secret
  generate_secret = true

  # OAuth configuration
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]

  # Callback and logout URLs
  callback_urls = ["https://${var.gitlab_domain}/users/auth/openid_connect/callback"]
  logout_urls   = ["https://${var.gitlab_domain}"]

  # Token validity (matches existing gateway client pattern)
  access_token_validity  = 60 # minutes
  id_token_validity      = 60 # minutes
  refresh_token_validity = 30 # days

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }

  # Prevent Terraform from detecting drift on secret rotation
  lifecycle {
    ignore_changes = [generate_secret]
  }
}
