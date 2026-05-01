# =============================================================================
# API Gateway HTTP API v2
# =============================================================================
# HTTP API v2 chosen over REST API v1: ~71% cheaper, lower latency, sufficient
# for webhook ingress. Separate from the Bedrock Gateway API (different auth
# model, different blast radius).
# =============================================================================

resource "aws_apigatewayv2_api" "webhook" {
  name          = "${local.name_prefix}-webhook-ingress"
  protocol_type = "HTTP"
  description   = "Webhook ingress for hosted ADP - receives GitHub webhooks"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.webhook.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gateway.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      requestTime    = "$context.requestTime"
      httpMethod     = "$context.httpMethod"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      protocol       = "$context.protocol"
      responseLength = "$context.responseLength"
      integrationErr = "$context.integrationErrorMessage"
    })
  }
}

resource "aws_cloudwatch_log_group" "api_gateway" {
  name              = "/aws/apigateway/${local.name_prefix}-webhook-ingress"
  retention_in_days = 14
}

# Custom domain placeholder — uncomment and configure when domain is ready
# resource "aws_apigatewayv2_domain_name" "webhook" {
#   domain_name = "webhooks.${var.domain_name}"
#
#   domain_name_configuration {
#     certificate_arn = var.acm_certificate_arn
#     endpoint_type   = "REGIONAL"
#     security_policy = "TLS_1_2"
#   }
# }
#
# resource "aws_apigatewayv2_api_mapping" "webhook" {
#   api_id      = aws_apigatewayv2_api.webhook.id
#   domain_name = aws_apigatewayv2_domain_name.webhook.id
#   stage       = aws_apigatewayv2_stage.default.id
# }
