mock_provider "aws" {
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/shared-tools-lambda" } }
  mock_resource "aws_dynamodb_table" { defaults = { arn = "arn:aws:dynamodb:us-east-1:123456789012:table/shared-tools-operations" } }
  mock_resource "aws_cloudwatch_log_group" { defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/shared-tools" } }
  mock_resource "aws_lambda_function" { defaults = { invoke_arn = "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/arn:aws:lambda:us-east-1:123456789012:function:shared-tools/invocations" } }
}
mock_provider "kubernetes" {}
variables { aws_account_id = "123456789012" }
run "disabled_creates_nothing" {
  command = plan
  assert {
    condition     = length(aws_lambda_function.service) == 0 && length(aws_dynamodb_table.operations) == 0 && length(aws_api_gateway_resource.websearch) == 0 && length(aws_api_gateway_resource.code_interpreter) == 0
    error_message = "Shared tools must remain off by default."
  }
}
run "scoped_shared_routes_and_disabled_admission" {
  command = plan
  variables {
    enabled                  = true
    image_uri                = "123456789012.dkr.ecr.us-east-1.amazonaws.com/shared-tools@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
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
  }
  assert {
    condition     = aws_api_gateway_method.websearch[0].authorization == "AWS_IAM" && aws_api_gateway_method.code_interpreter[0].authorization == "AWS_IAM" && aws_lambda_function.service[0].environment[0].variables.ADP_WEBSEARCH_ENABLED == "false" && aws_lambda_function.service[0].environment[0].variables.ADP_CODE_INTERPRETER_ENABLED == "false"
    error_message = "Shared routes are IAM-only and paid tools stay off."
  }
  assert {
    condition     = aws_dynamodb_table.operations[0].deletion_protection_enabled && aws_lambda_permission.websearch[0].source_account == "123456789012"
    error_message = "Preserve durable claims and exact route permissions."
  }
}
run "mutable_image_rejected" {
  command = plan
  variables { image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/shared-tools:latest" }
  expect_failures = [var.image_uri]
}

run "wildcard_authority_rejected" {
  command = plan
  variables { authority_invoke_arns = ["arn:aws:execute-api:us-east-1:123456789012:abc123/*/*/*"] }
  expect_failures = [var.authority_invoke_arns]
}

run "dedicated_gateway_pins_connector_and_target_restrictions" {
  command = plan
  variables {
    enabled                   = true
    websearch_create_gateway  = true
    websearch_enabled         = true
    websearch_target_includes = ["source.example"]
    websearch_target_excludes = ["blocked.example"]
    image_uri                 = "123456789012.dkr.ecr.us-east-1.amazonaws.com/shared-tools@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
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
  }
  assert {
    condition     = aws_bedrockagentcore_gateway.websearch[0].authorizer_type == "AWS_IAM" && aws_bedrockagentcore_gateway_target.websearch[0].target_configuration[0].mcp[0].connector[0].source[0].connector_id == "web-search" && aws_bedrockagentcore_gateway_target.websearch[0].target_configuration[0].mcp[0].connector[0].source[0].version == "1.2.0"
    error_message = "The shared IAM gateway must use pinned Web Search connector 1.2.0."
  }
  assert {
    condition     = jsondecode(aws_bedrockagentcore_gateway_target.websearch[0].target_configuration[0].mcp[0].connector[0].configuration[0].parameter_values).domainFilter.include == ["source.example"] && jsondecode(aws_bedrockagentcore_gateway_target.websearch[0].target_configuration[0].mcp[0].connector[0].configuration[0].parameter_values).domainFilter.exclude == ["blocked.example"]
    error_message = "Target-level filters remain in place independently of Task filters."
  }
}
