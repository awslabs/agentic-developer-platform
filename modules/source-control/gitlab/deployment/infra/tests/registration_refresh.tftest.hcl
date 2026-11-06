mock_provider "aws" {
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
}

variables {
  account_id  = "123456789012"
  aws_region  = "us-west-2"
  environment = "test"
  repository  = "example/platform"
}

run "no_implicit_refresh" {
  command = plan
  assert {
    condition     = length(aws_iam_policy.registration_refresh) == 0 && length(aws_iam_role_policy_attachment.registration_refresh) == 0
    error_message = "Identity setup must not grant inventory reads."
  }
}

run "refresh_reads_without_mutations" {
  command = plan
  variables {
    registration_refresh = {
      role_arns                   = ["arn:aws:iam::123456789012:role/adp-test-gitlab-instance"]
      instance_profile_arn        = "arn:aws:iam::123456789012:instance-profile/adp-test-gitlab-instance"
      instance_arn                = "arn:aws:ec2:us-west-2:123456789012:instance/i-0123456789abcdef0"
      secret_arns                 = ["arn:aws:secretsmanager:us-west-2:123456789012:secret:adp/test/gitlab/ca-chain-abc123"]
      parameter_arns              = ["arn:aws:ssm:us-west-2:123456789012:parameter/adp/test/gitlab/url"]
      maintenance_window_arn      = "arn:aws:ssm:us-west-2:123456789012:maintenancewindow/mw-0123456789abcdef0"
      maintenance_window_task_arn = "arn:aws:ssm:us-west-2:123456789012:windowtask/40b1a9b2-727b-4ede-ad40-acbdd2e76866"
    }
  }
  assert {
    condition = alltrue(flatten([for statement in jsondecode(aws_iam_policy.registration_refresh[0].policy).Statement : [
      for action in flatten([statement.Action]) : can(regex("^[a-z0-9-]+:(Get|Describe|List)[A-Za-z]+$", action)) && action != "secretsmanager:GetSecretValue"
    ]]))
    error_message = "Refresh must not grant IAM writes, instance changes, PassRole or Secrets Manager values."
  }
  assert {
    condition = alltrue([for statement in jsondecode(aws_iam_policy.registration_refresh[0].policy).Statement :
      statement.Resource == "*" ? try(statement.Condition.StringEquals["aws:RequestedRegion"] == "us-west-2", false) : statement.Resource != null
    ])
    error_message = "No optional null resources; unscoped regional APIs must retain a region constraint."
  }
  assert {
    condition = toset([for statement in jsondecode(aws_iam_policy.registration_refresh[0].policy).Statement : statement.Resource
      if contains(flatten([statement.Action]), "ssm:GetMaintenanceWindowTask")
      ]) == toset([
      "arn:aws:ssm:us-west-2:123456789012:maintenancewindow/mw-0123456789abcdef0",
      "arn:aws:ssm:us-west-2:123456789012:windowtask/40b1a9b2-727b-4ede-ad40-acbdd2e76866",
    ])
    error_message = "GetMaintenanceWindowTask must retain the documented window allow and add only the exact task ARN observed in live authorization."
  }
}

run "reject_foreign_maintenance_task" {
  command = plan
  variables {
    registration_refresh = {
      role_arns                   = ["arn:aws:iam::123456789012:role/adp-test-gitlab-instance"]
      instance_profile_arn        = "arn:aws:iam::123456789012:instance-profile/adp-test-gitlab-instance"
      instance_arn                = "arn:aws:ec2:us-west-2:123456789012:instance/i-0123456789abcdef0"
      secret_arns                 = ["arn:aws:secretsmanager:us-west-2:123456789012:secret:adp/test/gitlab/ca-chain-abc123"]
      parameter_arns              = ["arn:aws:ssm:us-west-2:123456789012:parameter/adp/test/gitlab/url"]
      maintenance_window_task_arn = "arn:aws:ssm:us-west-2:999999999999:windowtask/40b1a9b2-727b-4ede-ad40-acbdd2e76866"
    }
  }
  expect_failures = [var.registration_refresh]
}

run "reject_wildcard_role" {
  command = plan
  variables {
    registration_refresh = {
      role_arns            = ["arn:aws:iam::123456789012:role/*"]
      instance_profile_arn = "arn:aws:iam::123456789012:instance-profile/adp-test-gitlab-instance"
      instance_arn         = "arn:aws:ec2:us-west-2:123456789012:instance/i-0123456789abcdef0"
      secret_arns          = ["arn:aws:secretsmanager:us-west-2:123456789012:secret:adp/test/gitlab/ca-chain-abc123"]
      parameter_arns       = ["arn:aws:ssm:us-west-2:123456789012:parameter/adp/test/gitlab/url"]
    }
  }
  expect_failures = [var.registration_refresh]
}

run "reject_foreign_parameter" {
  command = plan
  variables {
    registration_refresh = {
      role_arns            = ["arn:aws:iam::123456789012:role/adp-test-gitlab-instance"]
      instance_profile_arn = "arn:aws:iam::123456789012:instance-profile/adp-test-gitlab-instance"
      instance_arn         = "arn:aws:ec2:us-west-2:123456789012:instance/i-0123456789abcdef0"
      secret_arns          = ["arn:aws:secretsmanager:us-west-2:123456789012:secret:adp/test/gitlab/ca-chain-abc123"]
      parameter_arns       = ["arn:aws:ssm:us-west-2:999999999999:parameter/adp/test/gitlab/url"]
    }
  }
  expect_failures = [var.registration_refresh]
}
