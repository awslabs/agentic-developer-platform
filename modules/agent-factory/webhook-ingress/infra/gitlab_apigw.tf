# =============================================================================
# API Gateway Route: POST /gitlab (Issue #3324)
# =============================================================================
# GitLab webhook route — token validation happens inside the Lambda (same
# pattern as GitHub's HMAC validation). NONE auth at the API GW layer.
# =============================================================================

resource "aws_api_gateway_resource" "gitlab" {
  count       = var.gitlab_webhook_enabled ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.webhook.id
  parent_id   = aws_api_gateway_rest_api.webhook.root_resource_id
  path_part   = "gitlab"
}

resource "aws_api_gateway_method" "post_gitlab" {
  count         = var.gitlab_webhook_enabled ? 1 : 0
  rest_api_id   = aws_api_gateway_rest_api.webhook.id
  resource_id   = aws_api_gateway_resource.gitlab[0].id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "gitlab_webhook" {
  count                   = var.gitlab_webhook_enabled ? 1 : 0
  rest_api_id             = aws_api_gateway_rest_api.webhook.id
  resource_id             = aws_api_gateway_resource.gitlab[0].id
  http_method             = aws_api_gateway_method.post_gitlab[0].http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.gitlab_webhook[0].invoke_arn
}

# Per-method throttling for POST /gitlab — same limits as GitHub webhook
resource "aws_api_gateway_method_settings" "throttle_gitlab" {
  count       = var.gitlab_webhook_enabled ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.webhook.id
  stage_name  = aws_api_gateway_stage.dev.stage_name
  method_path = "gitlab/POST"

  settings {
    throttling_rate_limit  = 100
    throttling_burst_limit = 200
    logging_level          = "INFO"
    metrics_enabled        = true
  }
}
