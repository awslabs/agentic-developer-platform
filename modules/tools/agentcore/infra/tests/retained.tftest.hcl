mock_provider "aws" {
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/fictional-tools" } }
  mock_resource "aws_dynamodb_table" { defaults = { arn = "arn:aws:dynamodb:us-east-1:123456789012:table/fictional-tools" } }
  mock_resource "aws_sqs_queue" { defaults = { arn = "arn:aws:sqs:us-east-1:123456789012:fictional-browser.fifo", url = "https://sqs.us-east-1.amazonaws.com/123456789012/fictional-browser.fifo" } }
  mock_resource "aws_cloudwatch_log_group" { defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/fictional-tools" } }
  mock_resource "aws_cloudwatch_event_rule" { defaults = { arn = "arn:aws:events:us-east-1:123456789012:rule/fictional-cleanup" } }
  mock_resource "aws_lambda_function" { defaults = { arn = "arn:aws:lambda:us-east-1:123456789012:function:fictional-tools", invoke_arn = "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/arn:aws:lambda:us-east-1:123456789012:function:fictional-tools/invocations" } }
}
mock_provider "kubernetes" {}

variables {
  aws_account_id            = "123456789012"
  enabled                   = true
  retain_legacy_routes      = true
  worker_role_arns          = ["arn:aws:iam::123456789012:role/fictional-worker"]
  browser_http_enabled      = true
  browser_service_image     = "123456789012.dkr.ecr.us-east-1.amazonaws.com/browser@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  browser_namespace         = "fictional-agents"
  browser_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/fictional"
  browser_oidc_issuer       = "https://oidc.eks.us-east-1.amazonaws.com/id/fictional"
  image_uri                 = "123456789012.dkr.ecr.us-east-1.amazonaws.com/tools@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  rest_api_id               = "abc123fict9"
  tools_parent_resource_id  = "fictional-parent"
  api_execution_arn         = "arn:aws:execute-api:us-east-1:123456789012:abc123fict9"
  stage_name                = "dev"
  authority_endpoint        = "https://gateway.example/internal/v1/agent/task"
  authority_invoke_arns = [
    "arn:aws:execute-api:us-east-1:123456789012:abc123fict9/dev/POST/internal/v1/agent/task/tool-authorize",
    "arn:aws:execute-api:us-east-1:123456789012:abc123fict9/dev/POST/internal/v1/agent/task/artifact"
  ]
}

run "existing_installation_retains_api_routes_by_default" {
  command = plan
  assert {
    condition = alltrue([
      length(aws_api_gateway_resource.browser) == 1, length(aws_api_gateway_method.browser) == 1,
      length(aws_api_gateway_integration.browser) == 1, length(aws_api_gateway_resource.code_interpreter) == 1,
      length(aws_api_gateway_method.code_interpreter) == 1, length(aws_api_gateway_integration.code_interpreter) == 1,
      length(aws_api_gateway_resource.websearch) == 1, length(aws_api_gateway_method.websearch) == 1,
      length(aws_api_gateway_integration.websearch) == 1
    ])
    error_message = "Existing installations must retain all nine API resources by default."
  }
}

run "already_absent_api_routes_are_not_recreated" {
  command = plan
  variables { retain_legacy_api_routes = false }
  assert {
    condition = alltrue([
      length(aws_api_gateway_resource.browser) == 0, length(aws_api_gateway_method.browser) == 0,
      length(aws_api_gateway_integration.browser) == 0, length(aws_api_gateway_resource.code_interpreter) == 0,
      length(aws_api_gateway_method.code_interpreter) == 0, length(aws_api_gateway_integration.code_interpreter) == 0,
      length(aws_api_gateway_resource.websearch) == 0, length(aws_api_gateway_method.websearch) == 0,
      length(aws_api_gateway_integration.websearch) == 0
    ])
    error_message = "The explicit retirement mode must never recreate already-absent API routes."
  }
  assert {
    condition = alltrue([
      length(aws_lambda_permission.browser) == 1, length(aws_lambda_permission.code_interpreter) == 1,
      length(aws_lambda_permission.websearch) == 1, length(aws_cloudwatch_log_group.browser) == 1,
      length(aws_iam_role.browser_gateway) == 1, length(aws_iam_role_policy.browser_gateway) == 1,
      length(aws_lambda_function.browser) == 1, length(aws_iam_role_policy.browser_worker) == 1,
      length(aws_iam_role_policy.code_interpreter_worker) == 1, length(aws_iam_role_policy.websearch_worker) == 1
    ])
    error_message = "Retiring absent API routes must preserve all ten live historical resources."
  }
  assert {
    condition = alltrue([for policy in [
      aws_iam_role_policy.browser_worker["arn:aws:iam::123456789012:role/fictional-worker"].policy,
      aws_iam_role_policy.code_interpreter_worker["arn:aws:iam::123456789012:role/fictional-worker"].policy,
      aws_iam_role_policy.websearch_worker["arn:aws:iam::123456789012:role/fictional-worker"].policy
    ] : jsondecode(policy).Statement[0].Effect == "Deny" && jsondecode(policy).Statement[0].Action == "execute-api:Invoke"])
    error_message = "The exact historical worker policy keys and direct-route denial must remain."
  }
}
