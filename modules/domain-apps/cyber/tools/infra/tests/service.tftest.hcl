mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/cyber-tools-lambda" }
  }
  mock_resource "aws_dynamodb_table" {
    defaults = { arn = "arn:aws:dynamodb:us-east-1:123456789012:table/cyber-tools-operations" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/cyber-tools" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { invoke_arn = "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/arn:aws:lambda:us-east-1:123456789012:function:cyber-tools/invocations" }
  }
}
variables { aws_account_id = "123456789012" }
run "disabled_creates_nothing" {
  command = plan
  assert {
    condition     = length(aws_lambda_function.service) == 0 && length(aws_dynamodb_table.operations) == 0 && length(aws_api_gateway_resource.cyber) == 0 && length(aws_iam_role_policy.worker) == 0 && length(aws_api_gateway_resource.websearch) == 0 && length(aws_api_gateway_resource.code_interpreter) == 0
    error_message = "The tools service must be opt-in."
  }
}
run "enabled_route_is_scoped_and_image_is_immutable" {
  command = plan
  variables {
    enabled                     = true
    websearch_enabled           = true
    websearch_gateway_url       = "https://example.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
    websearch_gateway_arn       = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/example"
    websearch_target            = "websearch-target"
    vpc_id                      = "vpc-0123456789abcdef0"
    endpoint_security_group_ids = ["sg-0123456789abcdef0"]
    subnet_ids                  = ["subnet-0123456789abcdef0"]
    image_uri                   = "123456789012.dkr.ecr.us-east-1.amazonaws.com/cyber-tools@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    rest_api_id                 = "abc123"
    tools_parent_resource_id    = "tools123"
    api_execution_arn           = "arn:aws:execute-api:us-east-1:123456789012:abc123"
    stage_name                  = "dev"
    worker_role_arns            = ["arn:aws:iam::123456789012:role/task-worker"]
    authority_endpoint          = "https://gateway.example/internal/v1/agent/task"
    authority_invoke_arns = [
      "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/POST/internal/v1/agent/task/tool-authorize",
      "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/POST/internal/v1/agent/task/artifact"
    ]
    sample_bucket_arn = "arn:aws:s3:::sample-bucket"
  }
  assert {
    condition     = aws_api_gateway_method.common_crawl[0].authorization == "AWS_IAM" && aws_lambda_permission.common_crawl[0].source_arn == "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/POST/tools/cyber/common-crawl"
    error_message = "Archive Lambda invocation must be restricted to the exact route."
  }
  assert {
    condition     = aws_api_gateway_method.websearch[0].authorization == "AWS_IAM" && aws_lambda_permission.websearch[0].source_arn == "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/POST/tools/websearch" && jsondecode(aws_iam_role_policy.websearch_gateway[0].policy).Statement[0].Resource == "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/example" && jsondecode(aws_iam_role_policy.websearch_worker["arn:aws:iam::123456789012:role/task-worker"].policy).Statement[0].Resource == "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/POST/tools/websearch"
    error_message = "Web Search must use a scoped IAM route and gateway."
  }
  assert {
    condition     = length(aws_security_group.service) == 1 && length(aws_security_group.service[0].ingress) == 0 && one(aws_security_group.service[0].egress).from_port == 443 && one(aws_security_group.service[0].egress).to_port == 443
    error_message = "Service-owned networking must expose no ingress and only HTTPS egress."
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.endpoints["sg-0123456789abcdef0"].from_port == 443 && aws_vpc_security_group_ingress_rule.endpoints["sg-0123456789abcdef0"].to_port == 443 && aws_vpc_security_group_ingress_rule.endpoints["sg-0123456789abcdef0"].ip_protocol == "tcp"
    error_message = "Private endpoint access must remain HTTPS only."
  }
  assert {
    condition     = aws_api_gateway_method.cyber[0].authorization == "AWS_IAM" && aws_api_gateway_integration.cyber[0].type == "AWS_PROXY"
    error_message = "Cyber route requires IAM and the isolated Lambda proxy."
  }
  assert {
    condition     = aws_api_gateway_method.code_interpreter[0].authorization == "AWS_IAM" && aws_lambda_permission.code_interpreter[0].source_arn == "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/POST/tools/code-interpreter" && aws_lambda_function.service[0].environment[0].variables["ADP_CODE_INTERPRETER_ENABLED"] == "false"
    error_message = "The new exact-route IAM integration must be disabled by default."
  }
  assert {
    condition     = aws_lambda_permission.api[0].source_arn == "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/POST/tools/cyber" && aws_lambda_permission.api[0].source_account == "123456789012"
    error_message = "Lambda permission must name exactly this API stage and route."
  }
  assert {
    condition     = aws_dynamodb_table.operations[0].deletion_protection_enabled && aws_dynamodb_table.operations[0].hash_key == "event_id" && aws_dynamodb_table.operations[0].range_key == "arrived_at"
    error_message = "Operation claims require an independently protected table."
  }
  assert {
    condition     = aws_lambda_function.service[0].environment[0].variables["ADP_TASK_CYBER_ENABLED"] == "false"
    error_message = "Provisioning must not implicitly enable operation admission."
  }

}
run "mutable_image_is_rejected" {
  command = plan
  variables { image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/cyber-tools:latest" }
  expect_failures = [var.image_uri]
}
run "wildcard_authority_is_rejected" {
  command = plan
  variables { authority_invoke_arns = ["arn:aws:execute-api:us-east-1:123456789012:abc123/*/*/*"] }
  expect_failures = [var.authority_invoke_arns]
}

run "wildcard_backend_queue_is_rejected" {
  command = plan
  variables { queue_arns = ["arn:aws:sqs:us-east-1:123456789012:*"] }
  expect_failures = [var.queue_arns]
}

run "dedicated_gateway_pins_connector_and_target_restrictions" {
  command = plan
  variables {
    enabled                   = true
    websearch_create_gateway  = true
    websearch_enabled         = true
    websearch_target_includes = ["source.example"]
    websearch_target_excludes = ["blocked.example"]
    image_uri                 = "123456789012.dkr.ecr.us-east-1.amazonaws.com/cyber-tools@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
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
    condition     = aws_bedrockagentcore_gateway.websearch[0].authorizer_type == "AWS_IAM" && aws_bedrockagentcore_gateway_target.websearch[0].target_configuration[0].mcp[0].connector[0].source[0].connector_id == "web-search" && aws_bedrockagentcore_gateway_target.websearch[0].target_configuration[0].mcp[0].connector[0].source[0].version == "1.2.0"
    error_message = "A dedicated gateway must be IAM only with a version-pinned Web Search connector."
  }
  assert {
    condition     = jsondecode(aws_bedrockagentcore_gateway_target.websearch[0].target_configuration[0].mcp[0].connector[0].configuration[0].parameter_values).domainFilter.include == ["source.example"] && jsondecode(aws_bedrockagentcore_gateway_target.websearch[0].target_configuration[0].mcp[0].connector[0].configuration[0].parameter_values).domainFilter.exclude == ["blocked.example"]
    error_message = "Target-level filters must be preserved independently of caller filters."
  }
}
