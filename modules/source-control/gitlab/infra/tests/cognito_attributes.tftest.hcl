# Offline plan: verify client permissions without contacting Cognito.
mock_provider "aws" {}

variables {
  environment          = "dev"
  gitlab_domain        = "gitlab.example.com"
  cognito_user_pool_id = "us-east-1_fixture"
  cognito_domain       = "adp-test"
  route53_zone_name    = "example.com"
}

run "gitlab_profile_writes_exclude_authority_attributes" {
  command = plan

  plan_options {
    target = [aws_cognito_user_pool_client.gitlab_oidc]
  }

  assert {
    condition     = toset(aws_cognito_user_pool_client.gitlab_oidc.write_attributes) == toset(["email", "name"])
    error_message = "GitLab may write only email/name; role, tenant, and other custom attributes must stay server-managed."
  }

  assert {
    condition = (
      aws_cognito_user_pool_client.gitlab_oidc.generate_secret &&
      aws_cognito_user_pool_client.gitlab_oidc.allowed_oauth_flows_user_pool_client &&
      toset(aws_cognito_user_pool_client.gitlab_oidc.allowed_oauth_flows) == toset(["code"]) &&
      toset(aws_cognito_user_pool_client.gitlab_oidc.allowed_oauth_scopes) == toset(["openid", "email", "profile"])
    )
    error_message = "GitLab must retain its server-side OIDC code flow and profile scopes without user-admin scope."
  }
}
