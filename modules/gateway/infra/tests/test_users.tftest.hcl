# Plan only: no AWS calls and no infrastructure creation.
mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_region" {
    defaults = { name = "us-east-1" }
  }
}
mock_provider "kubernetes" {}
mock_provider "helm" {}
mock_provider "tls" {}
mock_provider "random" {}
mock_provider "time" {}
mock_provider "archive" {}

override_data {
  target = data.terraform_remote_state.platform
  values = {
    outputs = {
      gateway_service_irsa_role_name = "fixture-gateway-service"
      gateway_service_irsa_role_arn  = "arn:aws:iam::123456789012:role/fixture-gateway-service"
    }
  }
}

override_module {
  target = module.cognito
  outputs = {
    cognito_user_pool_id = "us-east-1_fixture"
  }
}

variables {
  environment = "test"
  cost_center = "fixture"
}
run "dev_explicit_test_users_allowed" {
  command = plan
  variables {
    environment       = "dev"
    create_test_users = true
  }
  plan_options {
    # A root resource that depends on module.cognito (and therefore on
    # var.create_test_users) with no other remote-state inputs. The former
    # target, the gateway_cognito_read inline policy, moved into the
    # consolidated identity managed policy (#7944), whose for_each closure
    # would pull in every gateway dependency.
    target = [aws_ssm_parameter.cognito_user_pool_id]
  }
}
run "prod_test_users_rejected_at_gateway_boundary" {
  command = plan
  variables {
    environment       = "prod"
    create_test_users = true
  }
  plan_options {
    # A root resource that depends on module.cognito (and therefore on
    # var.create_test_users) with no other remote-state inputs. The former
    # target, the gateway_cognito_read inline policy, moved into the
    # consolidated identity managed policy (#7944), whose for_each closure
    # would pull in every gateway dependency.
    target = [aws_ssm_parameter.cognito_user_pool_id]
  }
  expect_failures = [var.create_test_users]
}
run "staging_test_users_rejected_at_gateway_boundary" {
  command = plan
  variables {
    environment       = "staging"
    create_test_users = true
  }
  plan_options {
    # A root resource that depends on module.cognito (and therefore on
    # var.create_test_users) with no other remote-state inputs. The former
    # target, the gateway_cognito_read inline policy, moved into the
    # consolidated identity managed policy (#7944), whose for_each closure
    # would pull in every gateway dependency.
    target = [aws_ssm_parameter.cognito_user_pool_id]
  }
  # Gateway currently also rejects staging as an environment independently.
  expect_failures = [var.environment]
}
