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

run "frontend_publisher_has_only_existing_static_site_access" {
  command = plan
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "adp-test-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E1234567890" }
  }
  assert {
    condition = jsondecode(aws_iam_role.frontend_deployment.assume_role_policy).Statement[0].Condition.StringEquals == {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
      "token.actions.githubusercontent.com:sub" = "repo:aws-e/adp:environment:adp-frontend-deploy-test"
    }
    error_message = "Frontend publication must require its own protected GitHub environment."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.frontend_deployment.policy).Statement : s if s.Sid == "PublishFrontendObjects"]).Resource == "arn:aws:s3:::adp-test-frontend/*"
    error_message = "Frontend publication must write only the deployed static bucket."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.frontend_deployment.policy).Statement : s if s.Sid == "RefreshFrontendCache"]).Resource == "arn:aws:cloudfront::123456789012:distribution/E1234567890"
    error_message = "Frontend publication must invalidate only its deployed distribution."
  }
  assert {
    condition     = !can(regex("iam:|eks:|lambda:|codebuild:|secretsmanager:|sts:AssumeRole", aws_iam_role_policy.frontend_deployment.policy))
    error_message = "Frontend publication gained backend or role-management authority."
  }
}
