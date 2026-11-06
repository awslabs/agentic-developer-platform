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
          parameter_values = jsonencode(length(var.websearch_target_includes) + length(var.websearch_target_excludes) == 0 ? {} : {
            domainFilter = merge(
              length(var.websearch_target_includes) > 0 ? { include = var.websearch_target_includes } : {},
              length(var.websearch_target_excludes) > 0 ? { exclude = var.websearch_target_excludes } : {}
            )
          })
        }
      }
    }
  }
  depends_on = [aws_iam_role_policy.websearch_connector]
}
output "websearch_gateway_url" { value = try(aws_bedrockagentcore_gateway.websearch[0].gateway_url, null) }
output "websearch_gateway_arn" { value = try(aws_bedrockagentcore_gateway.websearch[0].gateway_arn, null) }
