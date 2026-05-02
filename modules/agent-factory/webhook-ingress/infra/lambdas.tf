# =============================================================================
# GitHub Webhook Lambda
# =============================================================================
# Full handler: HMAC validation via Secrets Manager, tenant resolution,
# rate limiting, intent parsing, and SQS publish.
# Packaged by scripts/package-lambdas.sh → dist/github.zip
# =============================================================================

resource "aws_lambda_function" "github_webhook" {
  function_name = "${local.name_prefix}-github-webhook"
  description   = "GitHub webhook ingress - validates and queues events"
  role          = aws_iam_role.lambda_execution.arn

  filename         = "${path.module}/../dist/github.zip"
  source_code_hash = filebase64sha256("${path.module}/../dist/github.zip")
  handler          = "handler.handler"
  runtime          = var.lambda_runtime
  timeout          = var.lambda_timeout
  memory_size      = var.lambda_memory_size

  environment {
    variables = {
      ENVIRONMENT        = var.environment
      SUBMIT_QUEUE_URL   = aws_sqs_queue.agent_submit.url
      TENANT_TABLE       = aws_dynamodb_table.tenant_registry.name
      EVENTS_TABLE       = aws_dynamodb_table.webhook_events.name
      RATE_LIMITS_TABLE  = aws_dynamodb_table.rate_limits.name
      WEBHOOK_SECRET_ARN = aws_secretsmanager_secret.webhook_secret.arn
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name_prefix}-github-webhook"
  retention_in_days = 14
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.github_webhook.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.webhook.execution_arn}/*/*"
}
