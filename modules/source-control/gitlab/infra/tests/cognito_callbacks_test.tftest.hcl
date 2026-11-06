# =============================================================================
# Test: Cognito callback URLs include CloudFront URL when cloudfront_domain set
# =============================================================================

mock_provider "aws" {}

override_data {
  target = data.terraform_remote_state.platform
  values = {
    outputs = {
      vpc_id             = "vpc-00000000000000000"
      vpc_cidr_block     = "10.0.0.0/16"
      private_subnet_ids = ["subnet-00000000000000001", "subnet-00000000000000002"]
    }
  }
}

variables {
  environment          = "dev"
  gitlab_domain        = "gitlab.dev.adp.internal"
  cognito_user_pool_id = "us-east-1_TESTPOOL"
  cognito_domain       = "adp-dev"
  route53_zone_name    = "dev.adp.internal"
}

# -----------------------------------------------------------------------------
# Scenario: cloudfront_domain is set — both URLs present
# -----------------------------------------------------------------------------

run "callback_urls_include_cloudfront_when_set" {
  variables {
    cloudfront_domain = "d123abc.cloudfront.net"
  }

  command = plan

  assert {
    condition = contains(
      aws_cognito_user_pool_client.gitlab_oidc.callback_urls,
      "https://d123abc.cloudfront.net/gitlab/users/auth/openid_connect/callback"
    )
    error_message = "CloudFront callback URL should be present when cloudfront_domain is set"
  }

  assert {
    condition = contains(
      aws_cognito_user_pool_client.gitlab_oidc.callback_urls,
      "https://gitlab.dev.adp.internal/users/auth/openid_connect/callback"
    )
    error_message = "Internal ALB callback URL should always be present"
  }

  assert {
    condition = contains(
      aws_cognito_user_pool_client.gitlab_oidc.logout_urls,
      "https://d123abc.cloudfront.net/gitlab/"
    )
    error_message = "CloudFront logout URL should be present when cloudfront_domain is set"
  }

  assert {
    condition = contains(
      aws_cognito_user_pool_client.gitlab_oidc.logout_urls,
      "https://gitlab.dev.adp.internal"
    )
    error_message = "Internal ALB logout URL should always be present"
  }
}

# -----------------------------------------------------------------------------
# Scenario: cloudfront_domain is empty — only internal URLs present
# -----------------------------------------------------------------------------

run "callback_urls_exclude_cloudfront_when_empty" {
  variables {
    cloudfront_domain = ""
  }

  command = plan

  assert {
    condition     = length(aws_cognito_user_pool_client.gitlab_oidc.callback_urls) == 1
    error_message = "Only one callback URL should be present when cloudfront_domain is empty"
  }

  assert {
    condition = contains(
      aws_cognito_user_pool_client.gitlab_oidc.callback_urls,
      "https://gitlab.dev.adp.internal/users/auth/openid_connect/callback"
    )
    error_message = "Internal ALB callback URL should be the only URL when cloudfront_domain is empty"
  }

  assert {
    condition     = length(aws_cognito_user_pool_client.gitlab_oidc.logout_urls) == 1
    error_message = "Only one logout URL should be present when cloudfront_domain is empty"
  }

  assert {
    condition = contains(
      aws_cognito_user_pool_client.gitlab_oidc.logout_urls,
      "https://gitlab.dev.adp.internal"
    )
    error_message = "Internal ALB logout URL should be the only URL when cloudfront_domain is empty"
  }
}
