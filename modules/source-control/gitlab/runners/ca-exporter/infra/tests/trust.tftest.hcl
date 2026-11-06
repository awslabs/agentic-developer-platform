mock_provider "aws" {}

variables {
  account_id                 = "123456789012"
  aws_region                 = "us-east-1"
  environment                = "dev"
  trusted_operator_role_arns = ["arn:aws:iam::123456789012:role/operators/GitLabOperator"]
}

run "exact_operator_trust_and_instance_module_binding" {
  command = plan
  assert {
    condition = (
      output.runner_ca_exporter_role_name == "adp-dev-gitlab-runner-ca-exporter" &&
      aws_iam_role.runner_ca_exporter.max_session_duration == 3600 &&
      jsondecode(aws_iam_role.runner_ca_exporter.assume_role_policy) == {
        Version = "2012-10-17"
        Statement = [{
          Effect    = "Allow", Action = "sts:AssumeRole",
          Principal = { AWS = ["arn:aws:iam::123456789012:role/operators/GitLabOperator"] }
        }]
      }
    )
    error_message = "Only the selected operator role may assume the separate, one-hour CA exporter."
  }
}

run "environment_is_selected_instead_of_copied_from_dev" {
  command = plan
  variables {
    environment = "staging"
  }
  assert {
    condition = (
      output.runner_ca_exporter_role_name == "adp-staging-gitlab-runner-ca-exporter" &&
      aws_iam_role.runner_ca_exporter.tags.Environment == "staging"
    )
    error_message = "The exporter must belong to the selected environment."
  }
}

run "reject_empty_trust" {
  command = plan
  variables { trusted_operator_role_arns = [] }
  expect_failures = [var.trusted_operator_role_arns]
}

run "reject_other_account" {
  command = plan
  variables { trusted_operator_role_arns = ["arn:aws:iam::000000000000:role/Operator"] }
  expect_failures = [var.trusted_operator_role_arns]
}

run "reject_account_root" {
  command = plan
  variables { trusted_operator_role_arns = ["arn:aws:iam::123456789012:root"] }
  expect_failures = [var.trusted_operator_role_arns]
}

run "reject_wildcard_role" {
  command = plan
  variables { trusted_operator_role_arns = ["arn:aws:iam::123456789012:role/*"] }
  expect_failures = [var.trusted_operator_role_arns]
}

run "reject_session_principal" {
  command = plan
  variables { trusted_operator_role_arns = ["arn:aws:sts::123456789012:assumed-role/Operator/session"] }
  expect_failures = [var.trusted_operator_role_arns]
}

run "reject_user_principal" {
  command = plan
  variables { trusted_operator_role_arns = ["arn:aws:iam::123456789012:user/Operator"] }
  expect_failures = [var.trusted_operator_role_arns]
}

run "reject_self_trust" {
  command = plan
  variables { trusted_operator_role_arns = ["arn:aws:iam::123456789012:role/adp-dev-gitlab-runner-ca-exporter"] }
  expect_failures = [var.trusted_operator_role_arns]
}
