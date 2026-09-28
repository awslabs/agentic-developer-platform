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
  mock_resource "aws_lambda_function" {
    defaults = { invoke_arn = "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/arn:aws:lambda:us-east-1:123456789012:function:browser/invocations" }
  }
}
mock_provider "kubernetes" {}

variables { aws_account_id = "123456789012" }

run "browser_is_off_by_default" {
  command = plan
  assert {
    condition     = length(aws_lambda_function.browser) == 0 && length(kubernetes_deployment.browser) == 0 && length(aws_sqs_queue.browser) == 0
    error_message = "The HTTP browser must not be provisioned by default."
  }
}

run "browser_route_worker_and_consumer_are_scoped" {
  command = plan
  variables {
    enabled                   = true
    browser_http_enabled      = true
    browser_service_image     = "123456789012.dkr.ecr.us-east-1.amazonaws.com/browser@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    browser_namespace         = "custom-agents"
    browser_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/test"
    browser_oidc_issuer       = "https://oidc.eks.us-east-1.amazonaws.com/id/test"
    image_uri                 = "123456789012.dkr.ecr.us-east-1.amazonaws.com/cyber-tools@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    rest_api_id               = "abc123"
    tools_parent_resource_id  = "tools123"
    api_execution_arn         = "arn:aws:execute-api:us-east-1:123456789012:abc123"
    stage_name                = "dev"
    worker_role_arns          = ["arn:aws:iam::123456789012:role/task-worker"]
    authority_endpoint        = "https://gateway.example/internal/v1/agent/task"
    authority_invoke_arns = [
      "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/POST/internal/v1/agent/task/tool-authorize",
      "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/POST/internal/v1/agent/task/artifact"
    ]
    sample_bucket_arn = "arn:aws:s3:::sample-bucket"
  }
  assert {
    condition     = aws_api_gateway_method.browser[0].authorization == "AWS_IAM" && aws_lambda_permission.browser[0].source_arn == "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/POST/tools/browser"
    error_message = "The gateway must bind exact route and IAM caller."
  }
  assert {
    condition     = aws_dynamodb_table.browser[0].deletion_protection_enabled && aws_sqs_queue.browser[0].fifo_queue
    error_message = "The service requires single-owner sessions, durable claims and ordered dispatch."
  }
  assert {
    condition     = tostring(kubernetes_deployment.browser[0].spec[0].replicas) == "1"
    error_message = "Only one browser process may own interactive sessions."
  }
  assert {
    condition     = aws_lambda_function.browser[0].environment[0].variables["ADP_TASK_BROWSER_HTTP_ENABLED"] == "false"
    error_message = "Provisioning the route must not admit paid Browser starts."
  }
}
