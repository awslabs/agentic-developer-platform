mock_provider "aws" {
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_data "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/adp-test-gitlab-runner-ca-exporter" }
  }
}

variables {
  account_id  = "123456789012"
  aws_region  = "us-east-1"
  environment = "test"
  repository  = "example/platform"
}

run "only_exact_repository_and_gitlab_environment" {
  command = plan
  assert {
    condition = jsondecode(aws_iam_role.deployment.assume_role_policy).Statement == [{
      Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "repo:example/platform:environment:adp-gitlab-deploy-test"
      } }
    }]
    error_message = "Trust must select exactly one repository, GitLab environment and STS audience."
  }
  assert {
    condition     = aws_iam_role.deployment.name == "adp-test-gitlab-trusted-deployment"
    error_message = "Role name must satisfy the trusted-deployment action without borrowing another module's role."
  }
  assert {
    condition     = length(aws_iam_role_policy.assume_ca_exporter) == 0
    error_message = "Initial identity setup must not grant service permissions."
  }
}

run "delegate_only_to_bound_exporter" {
  command = plan
  variables { ca_exporter_role_name = "adp-test-gitlab-runner-ca-exporter" }
  assert {
    condition = jsondecode(aws_iam_role_policy.assume_ca_exporter[0].policy).Statement == [{
      Effect = "Allow", Action = "sts:AssumeRole", Resource = "arn:aws:iam::123456789012:role/adp-test-gitlab-runner-ca-exporter"
    }]
    error_message = "CA delegation may only assume the existing module exporter."
  }
}

run "reject_another_role" {
  command = plan
  variables { ca_exporter_role_name = "adp-test-gitlab-trusted-deployment" }
  expect_failures = [var.ca_exporter_role_name]
}

run "reject_repository_wildcard" {
  command = plan
  variables { repository = "example/*" }
  expect_failures = [var.repository]
}

run "reject_environment_wildcard" {
  command = plan
  variables { environment = "*" }
  expect_failures = [var.environment]
}
