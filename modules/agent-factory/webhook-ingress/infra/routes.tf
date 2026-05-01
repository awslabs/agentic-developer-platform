# =============================================================================
# API Gateway Routes
# =============================================================================
# POST /github → Lambda integration
# No authorizer — Lambda validates HMAC internally.
# =============================================================================

resource "aws_apigatewayv2_integration" "github_webhook" {
  api_id                 = aws_apigatewayv2_api.webhook.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.github_webhook.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "post_github" {
  api_id    = aws_apigatewayv2_api.webhook.id
  route_key = "POST /github"
  target    = "integrations/${aws_apigatewayv2_integration.github_webhook.id}"
}
