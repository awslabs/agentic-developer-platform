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

run "probe_requires_separate_trust_id" {
  command = plan
  variables {
    environment                             = "test"
    cost_center                             = "fixture"
    persona_model_probe_destination_enabled = true
    persona_model_probe_external_id         = "platform-probe:0123456789abcdef0123456789abcdef"
  }
  plan_options {
    target = [aws_iam_role.persona_model_probe_destination]
  }
  assert {
    condition     = jsondecode(aws_iam_role.persona_model_probe_destination[0].assume_role_policy).Statement[0].Condition.StringEquals["sts:ExternalId"] == var.persona_model_probe_external_id
    error_message = "Platform probe trust must require its dedicated ExternalId."
  }
}

run "probe_without_trust_id_is_refused" {
  command = plan
  variables {
    environment                             = "test"
    cost_center                             = "fixture"
    persona_model_probe_destination_enabled = true
  }
  plan_options {
    target = [aws_iam_role.persona_model_probe_destination]
  }
  expect_failures = [aws_iam_role.persona_model_probe_destination]
}
