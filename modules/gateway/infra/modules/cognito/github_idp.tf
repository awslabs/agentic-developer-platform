# =============================================================================
# GitHub OAuth Identity Provider (Issue #313)
# =============================================================================
# Adds GitHub as a federated OIDC identity provider to Cognito.
# GitHub does not implement full OIDC discovery, so endpoints are configured
# explicitly. Gated behind var.enable_github_oauth.
#
# Secret handling: the OAuth App's client_id + client_secret are NOT passed
# through Terraform variables — that would leave them in plaintext in TF
# state. Instead, the secret is pre-provisioned out-of-band in Secrets
# Manager at `adp/<env>/cognito/github-oauth-credentials` with a JSON body
# {"client_id":"...","client_secret":"..."} and read via a data source at
# apply-time. State still holds the resolved secret in the aws_cognito_
# identity_provider.github resource (Cognito requires it at create), but
# it is not written to tfvars, environment variables, or any git-tracked
# file.
# =============================================================================

# --- Read GitHub OAuth credentials from Secrets Manager ----------------------

data "aws_secretsmanager_secret" "github_oauth" {
  count = var.enable_github_oauth ? 1 : 0
  name  = "adp/${var.environment}/cognito/github-oauth-credentials"
}

data "aws_secretsmanager_secret_version" "github_oauth" {
  count     = var.enable_github_oauth ? 1 : 0
  secret_id = data.aws_secretsmanager_secret.github_oauth[0].id
}

locals {
  github_oauth = var.enable_github_oauth ? jsondecode(
    data.aws_secretsmanager_secret_version.github_oauth[0].secret_string
  ) : { client_id = "", client_secret = "" }
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
    client_id                     = local.github_oauth.client_id
    client_secret                 = local.github_oauth.client_secret
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
