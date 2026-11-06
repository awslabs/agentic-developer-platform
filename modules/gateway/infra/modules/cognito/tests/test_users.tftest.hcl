# Mocked plans only: never invoke permanent-password provisioners.
mock_provider "aws" {
  mock_data "aws_region" {
    defaults = { id = "us-east-1", name = "us-east-1" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
}
mock_provider "archive" {}
mock_provider "random" {}
mock_provider "null" {}
variables {
  environment = "prod"
  name_prefix = "fixture"
  kms_key_arn = "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111"
}
run "default_has_no_shared_credentials" {
  command = plan
  assert {
    condition     = length(aws_cognito_user.test_admin) == 0 && length(aws_cognito_user.test_user) == 0 && length(null_resource.test_admin_permanent_password) == 0 && length(null_resource.test_user_permanent_password) == 0 && length(aws_secretsmanager_secret_version.test_admin_credentials) == 0 && length(aws_secretsmanager_secret_version.test_user_credentials) == 0
    error_message = "Default installs must not create shared test identities, permanent passwords or secrets."
  }
}
run "dev_explicit_opt_in" {
  command = plan
  variables {
    environment       = "dev"
    create_test_users = true
  }
  assert {
    condition     = length(aws_cognito_user.test_admin) == 1 && length(aws_cognito_user.test_user) == 1
    error_message = "Explicit dev fixture opt-in must remain available."
  }
}
run "test_explicit_opt_in" {
  command = plan
  variables {
    environment       = "test"
    create_test_users = true
  }
  assert {
    condition     = length(aws_cognito_user.test_admin) == 1
    error_message = "Explicit test fixture opt-in must remain available."
  }
}
run "prod_test_users_rejected" {
  command = plan
  variables {
    create_test_users = true
  }
  expect_failures = [var.create_test_users]
}
run "staging_test_users_rejected" {
  command = plan
  variables {
    environment       = "staging"
    create_test_users = true
  }
  expect_failures = [var.create_test_users]
}
