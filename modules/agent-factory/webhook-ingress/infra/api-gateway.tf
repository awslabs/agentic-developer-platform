# =============================================================================
# API Gateway REST API v1
# =============================================================================
# REST API v1 chosen over HTTP API v2 to restore:
# - Direct WAFv2 association (rate-based rules, IP allowlist)
# - Per-method throttling (cap POST /github at 100 rps)
# - Resource policies (aws:SourceIp allowlist at the API layer)
#
# HTTP API v2's cost savings (~71%) are negligible at webhook-ingress volumes
# (low-frequency, GitHub retries on timeout). WAF + throttle > pennies saved.
# =============================================================================

resource "aws_api_gateway_rest_api" "webhook" {
  name        = "${local.name_prefix}-webhook-ingress"
  description = "Webhook ingress for hosted ADP - receives GitHub webhooks"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

# -----------------------------------------------------------------------------
# Resource policy stub — allow all for now; follow-up PR will restrict to
# GitHub's published meta/hooks IP ranges via aws:SourceIp condition.
# -----------------------------------------------------------------------------

resource "aws_api_gateway_rest_api_policy" "webhook" {
  rest_api_id = aws_api_gateway_rest_api.webhook.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = "*"
        Action    = "execute-api:Invoke"
        Resource  = "${aws_api_gateway_rest_api.webhook.execution_arn}/*"
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# Deployment + Stage
# -----------------------------------------------------------------------------

resource "aws_api_gateway_deployment" "webhook" {
  rest_api_id = aws_api_gateway_rest_api.webhook.id

  # Force redeployment when routes/integrations change
  # Issue #2152 review point I4: /agent/trigger IDs included so route is
  # deployed automatically on apply (not silently 403/404).
  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.github.id,
      aws_api_gateway_method.post_github.id,
      aws_api_gateway_integration.github_webhook.id,
      aws_api_gateway_resource.agent.id,
      aws_api_gateway_resource.agent_trigger.id,
      aws_api_gateway_method.post_agent_trigger.id,
      aws_api_gateway_integration.agent_trigger.id,
      aws_api_gateway_rest_api_policy.webhook.policy,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_method.post_github,
    aws_api_gateway_integration.github_webhook,
    aws_api_gateway_method.post_agent_trigger,
    aws_api_gateway_integration.agent_trigger,
  ]
}

resource "aws_api_gateway_stage" "dev" {
  deployment_id = aws_api_gateway_deployment.webhook.id
  rest_api_id   = aws_api_gateway_rest_api.webhook.id
  stage_name    = var.environment

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gateway.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      requestTime    = "$context.requestTime"
      httpMethod     = "$context.httpMethod"
      resourcePath   = "$context.resourcePath"
      status         = "$context.status"
      protocol       = "$context.protocol"
      responseLength = "$context.responseLength"
      integrationErr = "$context.integrationErrorMessage"
    })
  }
}

# -----------------------------------------------------------------------------
# Per-method throttling — cap POST /github to prevent one abusive caller from
# exhausting the account-level 10k rps quota.
# -----------------------------------------------------------------------------

resource "aws_api_gateway_method_settings" "throttle" {
  rest_api_id = aws_api_gateway_rest_api.webhook.id
  stage_name  = aws_api_gateway_stage.dev.stage_name
  method_path = "github/POST"

  settings {
    throttling_rate_limit  = 100
    throttling_burst_limit = 200
    logging_level          = "INFO"
    metrics_enabled        = true
  }
}

# Issue #2152: per-method throttling for POST /agent/trigger — tighter than
# /github (10 rps / 20 burst) since agent spawns are more expensive and
# unbounded spawn is a compute blowup risk.
resource "aws_api_gateway_method_settings" "throttle_agent_trigger" {
  rest_api_id = aws_api_gateway_rest_api.webhook.id
  stage_name  = aws_api_gateway_stage.dev.stage_name
  method_path = "agent/trigger/POST"

  settings {
    throttling_rate_limit  = 10
    throttling_burst_limit = 20
    logging_level          = "INFO"
    metrics_enabled        = true
  }
}

resource "aws_cloudwatch_log_group" "api_gateway" {
  name              = "/aws/apigateway/${local.name_prefix}-webhook-ingress"
  retention_in_days = 14
}
