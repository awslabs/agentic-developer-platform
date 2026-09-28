mock_provider "aws" {
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/cyber-tools-lambda" } }
  mock_resource "aws_cloudwatch_log_group" { defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/cyber-tools" } }
  mock_resource "aws_lambda_function" { defaults = { invoke_arn = "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/arn:aws:lambda:us-east-1:123456789012:function:cyber-tools/invocations" } }
}
variables { aws_account_id = "123456789012" }
run "disabled_creates_nothing" {
  command = plan
  assert {
    condition     = length(aws_lambda_function.service) == 0 && length(aws_api_gateway_resource.cyber) == 0 && length(aws_api_gateway_resource.common_crawl) == 0
    error_message = "Cyber must remain off by default."
  }
}
run "cyber_only_uses_shared_table_without_owning_it" {
  command = plan
  variables {
    enabled                  = true
    image_uri                = "123456789012.dkr.ecr.us-east-1.amazonaws.com/cyber-tools@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    rest_api_id              = "abc123"
    tools_parent_resource_id = "tools123"
    api_execution_arn        = "arn:aws:execute-api:us-east-1:123456789012:abc123"
    stage_name               = "dev"
    worker_role_arns         = ["arn:aws:iam::123456789012:role/task-worker"]
    authority_endpoint       = "https://gateway.example/internal/v1/agent/task"
    authority_invoke_arns = [
      "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/POST/internal/v1/agent/task/tool-authorize",
      "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/POST/internal/v1/agent/task/artifact"
    ]
    sample_bucket_arn     = "arn:aws:s3:::sample-bucket"
    operations_table_name = "adp-cyber-tools-operations"
    operations_table_arn  = "arn:aws:dynamodb:us-east-1:123456789012:table/adp-cyber-tools-operations"
  }
  assert {
    condition     = aws_api_gateway_method.cyber[0].authorization == "AWS_IAM" && aws_api_gateway_method.common_crawl[0].authorization == "AWS_IAM" && aws_lambda_function.service[0].environment[0].variables.CYBER_TOOLS_TABLE == "adp-cyber-tools-operations"
    error_message = "Cyber retains its IAM routes and uses the shared table."
  }
}

run "wildcard_backend_queue_rejected" {
  command = plan
  variables { queue_arns = ["arn:aws:sqs:us-east-1:123456789012:*"] }
  expect_failures = [var.queue_arns]
}
