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

run "gateway_client_lifecycle_is_pool_scoped" {
  command = plan
  variables {
    environment = "test"
    cost_center = "fixture"
  }
  plan_options {
    target = [aws_iam_role_policy.gateway_cognito_read]
  }
  assert {
    condition     = aws_iam_role_policy.gateway_cognito_read.role == "fixture-gateway-service"
    error_message = "Client lifecycle permissions must belong to the gateway role."
  }
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.gateway_cognito_read.policy).Statement :
      statement.Resource == "arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_fixture"
    ])
    error_message = "All Cognito authority must remain bound to the gateway's existing user pool."
  }
  assert {
    condition = length([
      for statement in jsondecode(aws_iam_role_policy.gateway_cognito_read.policy).Statement : statement
      if statement.Sid == "CognitoMachineClientLifecycle" && statement.Effect == "Allow" &&
      toset(statement.Action) == toset([
        "cognito-idp:CreateUserPoolClient",
        "cognito-idp:DescribeUserPoolClient",
        "cognito-idp:DeleteUserPoolClient"
      ])
    ]) == 1
    error_message = "Machine client lifecycle requires exactly create, protected describe and delete; no update or wildcard authority."
  }
}
