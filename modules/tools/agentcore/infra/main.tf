locals {
  active        = var.enabled ? 1 : 0
  function_name = "${var.name}-shared"
}
resource "aws_dynamodb_table" "operations" {
  count        = local.active
  name         = "${var.name}-operations"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "event_id"
  range_key    = "arrived_at"
  attribute {
    name = "event_id"
    type = "S"
  }
  attribute {
    name = "arrived_at"
    type = "S"
  }
  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true }
  deletion_protection_enabled = true
}
resource "aws_cloudwatch_log_group" "service" {
  count             = local.active
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 30
}
resource "aws_iam_role" "service" {
  count = local.active
  name  = "${var.name}-shared-lambda"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" }
  }] })
}
resource "aws_iam_role_policy" "service" {
  count = local.active
  name  = "shared-tools-scoped-access"
  role  = aws_iam_role.service[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.service[0].arn}:*" },
    { Effect = "Allow", Action = "execute-api:Invoke", Resource = sort(tolist(var.authority_invoke_arns)) },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:TransactWriteItems"], Resource = aws_dynamodb_table.operations[0].arn }
  ], length(var.subnet_ids) == 0 ? [] : [{ Effect = "Allow", Action = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets", "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"], Resource = "*" }]) })
}
resource "aws_security_group" "service" {
  count       = var.enabled && var.vpc_id != "" ? 1 : 0
  name        = "${var.name}-shared-lambda"
  description = "Shared tools Lambda: HTTPS egress, no ingress"
  vpc_id      = var.vpc_id
  ingress     = []
  egress {
    description = "HTTPS to Task authority and authorized AWS providers"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_lambda_function" "service" {
  count                          = local.active
  function_name                  = local.function_name
  role                           = aws_iam_role.service[0].arn
  package_type                   = "Image"
  image_uri                      = var.image_uri
  architectures                  = ["x86_64"]
  timeout                        = 28
  memory_size                    = 1024
  reserved_concurrent_executions = 10
  environment {
    variables = {
      ADP_TOOLS_WORKER_ROLES          = join(",", sort(tolist(var.worker_role_arns)))
      ADP_TOOLS_TABLE                 = aws_dynamodb_table.operations[0].name
      ADP_TASK_AUTHORITY_ENDPOINT     = var.authority_endpoint
      ADP_CODE_INTERPRETER_ENABLED    = tostring(var.code_interpreter_enabled)
      ADP_CODE_INTERPRETER_ID         = var.code_interpreter_identifier
      ADP_WEBSEARCH_ENABLED           = tostring(var.websearch_enabled)
      ADP_WEBSEARCH_GATEWAY_URL       = var.websearch_create_gateway ? aws_bedrockagentcore_gateway.websearch[0].gateway_url : var.websearch_gateway_url
      ADP_WEBSEARCH_REGION            = var.aws_region
      ADP_WEBSEARCH_TARGET            = var.websearch_create_gateway ? aws_bedrockagentcore_gateway_target.websearch[0].name : var.websearch_target
      ADP_WEBSEARCH_CONNECTOR_VERSION = "1.2.0"
    }
  }
  dynamic "vpc_config" {
    for_each = length(var.subnet_ids) == 0 ? [] : [1]
    content {
      subnet_ids         = var.subnet_ids
      security_group_ids = concat(tolist(var.security_group_ids), aws_security_group.service[*].id)
    }
  }
  lifecycle {
    precondition {
      condition     = var.api_execution_arn == "arn:aws:execute-api:${var.aws_region}:${var.aws_account_id}:${var.rest_api_id}" && alltrue([for arn in var.worker_role_arns : startswith(arn, "arn:aws:iam::${var.aws_account_id}:role/")])
      error_message = "Shared API and worker roles must belong to the confirmed account."
    }
    precondition {
      condition = var.authority_invoke_arns == toset([
        "${var.api_execution_arn}/${var.stage_name}/POST/internal/v1/agent/task/tool-authorize",
        "${var.api_execution_arn}/${var.stage_name}/POST/internal/v1/agent/task/artifact"
      ])
      error_message = "Authorize exactly both generic Task authority endpoints."
    }
    precondition {
      condition     = var.image_uri != "" && var.rest_api_id != "" && var.tools_parent_resource_id != "" && var.authority_endpoint != "" && length(var.worker_role_arns) > 0
      error_message = "Activation requires pinned image, existing shared API/tools parent, Task authority and explicit roles."
    }
    precondition {
      condition     = !var.websearch_enabled || var.websearch_create_gateway || (var.websearch_gateway_arn != "" && var.websearch_gateway_url != "" && var.websearch_target != "" && startswith(var.websearch_gateway_arn, "arn:aws:bedrock-agentcore:${var.aws_region}:${var.aws_account_id}:gateway/"))
      error_message = "Web Search requires a qualified same-account, same-region gateway."
    }
    precondition {
      condition     = !var.code_interpreter_enabled || (var.code_interpreter_identifier != "" && var.code_interpreter_arn == "arn:aws:bedrock-agentcore:${var.aws_region}:${var.aws_account_id}:code-interpreter-custom/${var.code_interpreter_identifier}")
      error_message = "Code Interpreter requires an exact same-account provider resource."
    }
    precondition {
      condition     = (length(var.subnet_ids) == 0) == (length(var.security_group_ids) == 0 && var.vpc_id == "")
      error_message = "Configure private networking completely or omit it."
    }
  }
  depends_on = [aws_iam_role_policy.service, aws_cloudwatch_log_group.service]
}
output "operations_table" { value = try(aws_dynamodb_table.operations[0].name, null) }
output "operations_table_arn" { value = try(aws_dynamodb_table.operations[0].arn, null) }
output "lambda_name" { value = try(aws_lambda_function.service[0].function_name, null) }
output "api_stage_deployment_required" {
  value       = var.enabled
  description = "The existing shared API owner publishes its stage separately."
}
