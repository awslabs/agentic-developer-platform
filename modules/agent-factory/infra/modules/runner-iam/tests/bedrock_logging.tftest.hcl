mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
}

variables {
  environment       = "test"
  name_prefix       = "adp-test"
  aws_region        = "eu-west-1"
  oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.eu-west-1.amazonaws.com/id/TEST"
  oidc_issuer       = "https://oidc.eks.eu-west-1.amazonaws.com/id/TEST"
}

run "dedicated_runner_name_avoids_legacy_codebuild_role" {
  command = plan
  variables {
    runner_role_name = "adp-test-agent-factory-runner-role"
  }
  assert {
    condition     = aws_iam_role.runner.name == "adp-test-agent-factory-runner-role"
    error_message = "The factory must use the selected IRSA role name."
  }
}
