mock_provider "external" {
  mock_data "external" { defaults = { result = { verified = "true" } } }
}
mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_data "aws_ssm_parameter" { defaults = { value = "mock" } }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/test" } }
  mock_resource "aws_iam_policy" { defaults = { arn = "arn:aws:iam::123456789012:policy/test" } }
}

variables {
  name_prefix         = "adp-test"
  environment         = "test"
  aws_region          = "us-east-1"
  cluster_name        = "adp-test-eks-cluster"
  repository          = "aws-e/adp"
  build_project_names = ["adp-test-gateway-build"]
}

run "default_browser_trust_stays_in_checks_environment" {
  command = plan
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "adp-test-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E1234567890" }
  }
  variables { enable_browser_checks = true }
  assert {
    condition = jsondecode(aws_iam_role.browser_checks[0].assume_role_policy).Statement[0].Condition.StringEquals == {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
      "token.actions.githubusercontent.com:sub" = ["repo:aws-e/adp:environment:adp-browser-checks-test"]
    }
    error_message = "Ordinary browser checks must not inherit deployment environment trust."
  }
  assert {
    condition     = aws_iam_role.browser_checks[0].max_session_duration == 3600
    error_message = "Ordinary browser checks retain the one-hour session maximum."
  }
}

run "product_regression_uses_explicit_environment_and_bounded_session" {
  command = plan
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "adp-test-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E1234567890" }
  }
  variables {
    enable_browser_checks            = true
    enable_product_regression_checks = true
  }
  assert {
    condition = jsondecode(aws_iam_role.browser_checks[0].assume_role_policy).Statement[0].Condition.StringEquals == {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
      "token.actions.githubusercontent.com:sub" = [
        "repo:aws-e/adp:environment:adp-browser-checks-test",
        "repo:aws-e/adp:environment:adp-gateway-deploy-test"
      ]
    }
    error_message = "Product regression may add only this repository's protected deployment environment."
  }
  assert {
    condition     = aws_iam_role.browser_checks[0].max_session_duration == 10800
    error_message = "Two-pass Product regression requires a bounded three-hour session."
  }
  assert {
    condition     = !can(regex("secretsmanager:Put|iam:|eks:|lambda:|codebuild:", aws_iam_role_policy.browser_checks[0].policy))
    error_message = "Browser fixture identity must not acquire deployment or secret-write actions."
  }
}
