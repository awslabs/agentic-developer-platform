resource "aws_api_gateway_resource" "websearch" {
  count       = local.active
  rest_api_id = var.rest_api_id
  parent_id   = var.tools_parent_resource_id
  path_part   = "websearch"
}
resource "aws_api_gateway_method" "websearch" {
  count         = local.active
  rest_api_id   = var.rest_api_id
  resource_id   = aws_api_gateway_resource.websearch[0].id
  http_method   = "POST"
  authorization = "AWS_IAM"
}
resource "aws_api_gateway_integration" "websearch" {
  count                   = local.active
  rest_api_id             = var.rest_api_id
  resource_id             = aws_api_gateway_resource.websearch[0].id
  http_method             = "POST"
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.service[0].invoke_arn
  timeout_milliseconds    = 29000
  depends_on              = [aws_api_gateway_method.websearch]
}
resource "aws_lambda_permission" "websearch" {
  count          = local.active
  statement_id   = "SharedApiWebSearchRoute"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.service[0].function_name
  principal      = "apigateway.amazonaws.com"
  source_arn     = "${var.api_execution_arn}/${var.stage_name}/POST/tools/websearch"
  source_account = var.aws_account_id
}
resource "aws_iam_role_policy" "websearch_worker" {
  for_each = var.enabled ? var.worker_role_arns : toset([])
  name     = "${var.name}-websearch-invoke"
  role     = element(reverse(split("/", each.value)), 0)
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "execute-api:Invoke", Resource = "${var.api_execution_arn}/${var.stage_name}/POST/tools/websearch"
  }] })
}
resource "aws_iam_role_policy" "websearch_gateway" {
  count = var.enabled && (var.websearch_gateway_arn != "" || var.websearch_create_gateway) ? 1 : 0
  name  = "${var.name}-websearch-gateway"
  role  = aws_iam_role.service[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "bedrock-agentcore:InvokeGateway", Resource = var.websearch_create_gateway ? aws_bedrockagentcore_gateway.websearch[0].gateway_arn : var.websearch_gateway_arn
  }] })
}
resource "aws_iam_role" "websearch_gateway" {
  count = var.enabled && var.websearch_create_gateway ? 1 : 0
  name  = "${var.name}-websearch-gateway"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "bedrock-agentcore.amazonaws.com" }
  }] })
}
resource "aws_iam_role_policy" "websearch_connector" {
  count = var.enabled && var.websearch_create_gateway ? 1 : 0
  name  = "websearch-connector"
  role  = aws_iam_role.websearch_gateway[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "bedrock-agentcore:InvokeGateway", Resource = aws_bedrockagentcore_gateway.websearch[0].gateway_arn },
    { Effect = "Allow", Action = "bedrock-agentcore:InvokeWebSearch", Resource = "arn:aws:bedrock-agentcore:${var.aws_region}:aws:tool/web-search.v1" }
  ] })
}
resource "aws_bedrockagentcore_gateway" "websearch" {
  count           = var.enabled && var.websearch_create_gateway ? 1 : 0
  name            = "${var.name}-websearch"
  role_arn        = aws_iam_role.websearch_gateway[0].arn
  authorizer_type = "AWS_IAM"
  protocol_type   = "MCP"
}
resource "aws_bedrockagentcore_gateway_target" "websearch" {
  count              = var.enabled && var.websearch_create_gateway ? 1 : 0
  name               = "${var.name}-websearch"
  gateway_identifier = aws_bedrockagentcore_gateway.websearch[0].gateway_id
  credential_provider_configuration {
    gateway_iam_role {}
  }
  target_configuration {
    mcp {
      connector {
        source {
          connector_id = "web-search"
          version      = "1.2.0"
        }
        configuration {
          name = "WebSearch"
          parameter_values = jsonencode({ domainFilter = {
            include = var.websearch_target_includes
            exclude = var.websearch_target_excludes
          } })
        }
      }
    }
  }
  depends_on = [aws_iam_role_policy.websearch_connector]
}
output "websearch_gateway_url" { value = try(aws_bedrockagentcore_gateway.websearch[0].gateway_url, null) }
output "websearch_gateway_arn" { value = try(aws_bedrockagentcore_gateway.websearch[0].gateway_arn, null) }
