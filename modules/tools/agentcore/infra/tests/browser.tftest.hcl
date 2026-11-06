mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/browser" }
  }
  mock_resource "aws_dynamodb_table" {
    defaults = { arn = "arn:aws:dynamodb:us-east-1:123456789012:table/browser-ops" }
  }
  mock_resource "aws_sqs_queue" {
    defaults = { arn = "arn:aws:sqs:us-east-1:123456789012:browser.fifo", url = "https://sqs.us-east-1.amazonaws.com/123456789012/browser.fifo" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/browser" }
  }
  mock_resource "aws_cloudwatch_event_rule" {
    defaults = { arn = "arn:aws:events:us-east-1:123456789012:rule/interpreter-cleanup" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:us-east-1:123456789012:function:browser", invoke_arn = "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/arn:aws:lambda:us-east-1:123456789012:function:browser/invocations" }
  }
}
mock_provider "kubernetes" {}

variables { aws_account_id = "123456789012" }

run "browser_is_off_by_default" {
  command = plan
  assert {
    condition     = length(kubernetes_deployment.browser) == 0 && length(aws_sqs_queue.browser) == 0
    error_message = "The HTTP browser must not be provisioned by default."
  }
}

run "browser_consumer_is_scoped_without_direct_route" {
  command = apply
  variables {
    enabled                   = true
    browser_http_enabled      = true
    browser_service_image     = "123456789012.dkr.ecr.us-east-1.amazonaws.com/browser@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    browser_namespace         = "custom-agents"
    browser_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/test"
    browser_oidc_issuer       = "https://oidc.eks.us-east-1.amazonaws.com/id/test"
    image_uri                 = "123456789012.dkr.ecr.us-east-1.amazonaws.com/cyber-tools@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    rest_api_id               = "abc123"
    api_execution_arn         = "arn:aws:execute-api:us-east-1:123456789012:abc123"
    stage_name                = "dev"
    authority_endpoint        = "https://gateway.example/internal/v1/agent/task"
    authority_invoke_arns = [
      "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/POST/internal/v1/agent/task/tool-authorize",
      "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/POST/internal/v1/agent/task/artifact"
    ]
  }
  assert {
    condition     = aws_dynamodb_table.browser[0].deletion_protection_enabled && aws_sqs_queue.browser[0].fifo_queue
    error_message = "The service requires single-owner sessions, durable claims and ordered dispatch."
  }
  assert {
    condition     = anytrue([for index in aws_dynamodb_table.browser[0].global_secondary_index : index.name == "cleanup-due"])
    error_message = "Browser cleanup must find expired sessions without a full-table scan."
  }
  assert {
    condition = anytrue([for statement in jsondecode(aws_iam_role_policy.service[0].policy).Statement :
      statement.Resource == aws_dynamodb_table.browser[0].arn &&
      try(toset(statement.Action) == toset(["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:TransactWriteItems", "dynamodb:ConditionCheckItem"]), false)
    ])
    error_message = "Browser transactions require condition checks scoped only to the browser operations table."
  }
  assert {
    condition = anytrue([for statement in jsondecode(aws_iam_role_policy.browser_service[0].policy).Statement :
      statement.Resource == aws_dynamodb_table.browser[0].arn &&
      try(contains(statement.Action, "dynamodb:ConditionCheckItem"), false)
      ]) && alltrue([for statement in jsondecode(aws_iam_role_policy.browser_service[0].policy).Statement :
      !try(contains(statement.Action, "dynamodb:ConditionCheckItem"), false) ||
      statement.Resource == aws_dynamodb_table.browser[0].arn
    ])
    error_message = "The FIFO consumer's atomic admission and dispatch fences require condition checks on its own table."
  }
  assert {
    condition     = tostring(kubernetes_deployment.browser[0].spec[0].replicas) == "1"
    error_message = "Only one browser process may own interactive sessions."
  }
  assert {
    condition     = anytrue([for env in kubernetes_deployment.browser[0].spec[0].template[0].spec[0].container[0].env : env.name == "AWS_DEFAULT_REGION" && env.value == "us-east-1"])
    error_message = "Boto3 needs AWS_DEFAULT_REGION outside the Lambda runtime."
  }
  assert {
    condition = aws_lambda_function.service[0].environment[0].variables["ADP_GATEWAY_BROWSER_READY"] == "false" && anytrue([
      for env in kubernetes_deployment.browser[0].spec[0].template[0].spec[0].container[0].env :
      env.name == "ADP_GATEWAY_BROWSER_READY" && env.value == "false"
    ])
    error_message = "Neither the Lambda nor the consumer admits Gateway browser work by default."
  }
}
