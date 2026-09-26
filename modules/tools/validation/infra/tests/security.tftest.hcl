mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-east-1" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
}
mock_provider "kubernetes" {}
variables {
  api_execution_arn         = "arn:aws:execute-api:us-east-1:123456789012:fixture"
  rest_api_id               = "fixture"
  stage_name                = "test"
  tools_parent_resource_id  = "tools"
  authority_endpoint        = "https://fixture.execute-api.us-east-1.amazonaws.com/test/internal/v1/agent/task"
  worker_role_arns          = ["arn:aws:iam::123456789012:role/worker"]
  cluster_name              = "fixture"
  cluster_endpoint          = "https://cluster.example"
  cluster_ca                = "Zml4dHVyZQ=="
  subnet_ids                = ["subnet-fixture"]
  security_group_ids        = ["sg-fixture"]
  image_uri                 = "123456789012.dkr.ecr.us-east-1.amazonaws.com/validation@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
run "disabled_has_no_runtime_or_grants" {
  command = plan
  assert {
    condition     = length(aws_lambda_function.service) == 0 && length(aws_iam_role_policy.worker) == 0 && length(aws_eks_access_entry.service) == 0
    error_message = "Disabled service must provision no runtime or grants."
  }
}
run "requires_qualified_isolation" {
  command = plan
  variables { enabled = true }
  expect_failures = [aws_lambda_function.service]
}
run "dedicated_iam_and_bounded_runtime" {
  command = plan
  variables {
    enabled             = true
    isolation_qualified = true
  }
  assert {
    condition     = aws_api_gateway_method.service[0].authorization == "AWS_IAM" && aws_lambda_function.service[0].timeout == 180 && aws_lambda_function_event_invoke_config.service[0].maximum_retry_attempts == 0
    error_message = "Validation must require IAM and bound asynchronous execution without blind retries."
  }
  assert {
    condition     = aws_lambda_function.service[0].environment[0].variables["ADP_VALIDATION_SERVICE_ENABLED"] == "false"
    error_message = "Provisioning must not enable the capability."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.worker["arn:aws:iam::123456789012:role/worker"].policy).Statement == [{ Effect = "Allow", Action = "execute-api:Invoke", Resource = "arn:aws:execute-api:us-east-1:123456789012:fixture/test/POST/tools/validation" }]
    error_message = "Workers get exactly one API route, never cluster, Lambda or job-store authority."
  }
  assert {
    condition     = aws_eks_access_entry.service[0].kubernetes_groups == toset(["adp-validation-service"]) && kubernetes_role_binding.service[0].metadata[0].namespace == "adp-codex-validation"
    error_message = "Dedicated service group must be scoped to the validation namespace."
  }
}
