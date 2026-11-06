mock_provider "aws" {
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/shared-tools-lambda" } }
  mock_resource "aws_dynamodb_table" { defaults = { arn = "arn:aws:dynamodb:us-east-1:123456789012:table/shared-tools-operations" } }
  mock_resource "aws_cloudwatch_log_group" { defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/shared-tools" } }
  mock_resource "aws_cloudwatch_event_rule" { defaults = { arn = "arn:aws:events:us-east-1:123456789012:rule/interpreter-cleanup" } }
  mock_resource "aws_lambda_function" { defaults = { arn = "arn:aws:lambda:us-east-1:123456789012:function:shared-tools", invoke_arn = "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/arn:aws:lambda:us-east-1:123456789012:function:shared-tools/invocations" } }
}
mock_provider "kubernetes" {}
variables { aws_account_id = "123456789012" }
run "disabled_creates_nothing" {
  command = plan
  assert {
    condition     = length(aws_lambda_function.service) == 0 && length(aws_dynamodb_table.operations) == 0
    error_message = "Shared tools must remain off by default."
  }
}
run "gateway_service_and_disabled_admission" {
  command = plan
  variables {
    enabled            = true
    image_uri          = "123456789012.dkr.ecr.us-east-1.amazonaws.com/shared-tools@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    rest_api_id        = "abc123"
    api_execution_arn  = "arn:aws:execute-api:us-east-1:123456789012:abc123"
    stage_name         = "dev"
    authority_endpoint = "https://gateway.example/internal/v1/agent/task"
    authority_invoke_arns = [
      "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/POST/internal/v1/agent/task/tool-authorize",
      "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/POST/internal/v1/agent/task/artifact"
    ]
  }
  assert {
    condition     = aws_lambda_function.service[0].environment[0].variables.ADP_WEBSEARCH_ENABLED == "false" && aws_lambda_function.service[0].environment[0].variables.ADP_CODE_INTERPRETER_ENABLED == "false"
    error_message = "Paid tools stay off until explicitly admitted through the gateway."
  }
  assert {
    condition     = !can(aws_lambda_function.service[0].environment[0].variables.ADP_TOOLS_WORKER_ROLES)
    error_message = "Only the signed gateway may authorize the shared Lambda; worker-role allowlists are retired."
  }
  assert {
    condition     = aws_dynamodb_table.operations[0].deletion_protection_enabled
    error_message = "Preserve durable claims without an API Gateway integration."
  }
  assert {
    condition = (
      anytrue([for index in aws_dynamodb_table.operations[0].global_secondary_index : index.name == "cleanup-due"]) &&
      aws_cloudwatch_event_rule.interpreter_cleanup[0].schedule_expression == "rate(1 minute)" &&
      aws_cloudwatch_event_target.interpreter_cleanup[0].input == jsonencode({ action = "reconcile-code-interpreter" }) &&
      aws_lambda_permission.interpreter_cleanup[0].principal == "events.amazonaws.com"
    )
    error_message = "Interpreter cleanup must be scheduled, owner-scoped and indexed."
  }
}
run "gateway_search_verification_grant" {
  command = apply
  variables {
    enabled            = true
    image_uri          = "123456789012.dkr.ecr.us-east-1.amazonaws.com/shared-tools@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    rest_api_id        = "abc123"
    api_execution_arn  = "arn:aws:execute-api:us-east-1:123456789012:abc123"
    stage_name         = "dev"
    authority_endpoint = "https://gateway.example/internal/v1/agent/task"
    authority_invoke_arns = [
      "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/POST/internal/v1/agent/task/tool-authorize",
      "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/POST/internal/v1/agent/task/artifact"
    ]
    tools_assertion_key_arn   = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001"
    tools_facade_role_arn     = "arn:aws:iam::123456789012:role/adp-dev-role-tools-facade"
    tools_reconciler_key_arn  = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000003"
    tools_reconciler_role_arn = "arn:aws:iam::123456789012:role/adp-dev-role-tools-reconciler"
  }
  assert {
    condition = (
      aws_lambda_function.service[0].environment[0].variables.ADP_TOOLS_ASSERTION_KEY_ARN == var.tools_assertion_key_arn &&
      aws_lambda_function.service[0].environment[0].variables.ADP_TOOLS_FACADE_ROLE_ARN == var.tools_facade_role_arn &&
      aws_lambda_function.service[0].environment[0].variables.ADP_TOOLS_RECONCILER_KEY_ARN == var.tools_reconciler_key_arn &&
      aws_lambda_function.service[0].environment[0].variables.ADP_TOOLS_RECONCILER_ROLE_ARN == var.tools_reconciler_role_arn &&
      length([for statement in jsondecode(aws_iam_role_policy.service[0].policy).Statement : statement if statement.Action == "kms:Verify" && statement.Resource == var.tools_assertion_key_arn]) == 1 &&
      length([for statement in jsondecode(aws_iam_role_policy.service[0].policy).Statement : statement if statement.Action == "kms:Verify" && statement.Resource == var.tools_reconciler_key_arn]) == 1 &&
      length([for statement in jsondecode(aws_iam_role_policy.service[0].policy).Statement : statement if statement.Action == "kms:Sign"]) == 0
    )
    error_message = "Gateway search may verify only the published signing key, never sign assertions."
  }
}
run "gateway_search_rejects_unpaired_signer" {
  command = plan
  variables {
    enabled                 = true
    tools_assertion_key_arn = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001"
  }
  expect_failures = [aws_lambda_function.service]
}
run "gateway_search_rejects_foreign_key" {
  command = plan
  variables { tools_assertion_key_arn = "arn:aws:kms:us-east-1:000000000000:key/00000000-0000-0000-0000-000000000001" }
  expect_failures = [var.tools_assertion_key_arn]
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
    api_execution_arn         = "arn:aws:execute-api:us-east-1:123456789012:abc123"
    stage_name                = "dev"
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

run "empty_filters_are_omitted" {
  command = plan
  variables {
    enabled                   = true
    websearch_create_gateway  = true
    websearch_enabled         = true
    websearch_target_includes = []
    websearch_target_excludes = []
    image_uri                 = "123456789012.dkr.ecr.us-east-1.amazonaws.com/shared-tools@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
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
    condition     = aws_bedrockagentcore_gateway_target.websearch[0].target_configuration[0].mcp[0].connector[0].configuration[0].parameter_values == "{}"
    error_message = "AWS rejects empty domain-filter lists; omit unset filters."
  }
}

run "include_only_omits_empty_exclude" {
  command = plan
  variables {
    enabled                   = true
    websearch_create_gateway  = true
    websearch_enabled         = true
    websearch_target_includes = ["source.example"]
    websearch_target_excludes = []
    image_uri                 = "123456789012.dkr.ecr.us-east-1.amazonaws.com/shared-tools@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
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
    condition     = aws_bedrockagentcore_gateway_target.websearch[0].target_configuration[0].mcp[0].connector[0].configuration[0].parameter_values == jsonencode({ domainFilter = { include = ["source.example"] } })
    error_message = "AWS rejects empty domain-filter lists; omit unset filters."
  }
}
