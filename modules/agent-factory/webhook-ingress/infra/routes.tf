# =============================================================================
# API Gateway Routes (REST API v1)
# =============================================================================
# POST /github → Lambda proxy integration
# No authorizer — Lambda validates HMAC internally.
# =============================================================================

resource "aws_api_gateway_resource" "github" {
  rest_api_id = aws_api_gateway_rest_api.webhook.id
  parent_id   = aws_api_gateway_rest_api.webhook.root_resource_id
  path_part   = "github"
}

resource "aws_api_gateway_method" "post_github" {
  rest_api_id   = aws_api_gateway_rest_api.webhook.id
  resource_id   = aws_api_gateway_resource.github.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "github_webhook" {
  rest_api_id             = aws_api_gateway_rest_api.webhook.id
  resource_id             = aws_api_gateway_resource.github.id
  http_method             = aws_api_gateway_method.post_github.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.github_webhook.invoke_arn
}
