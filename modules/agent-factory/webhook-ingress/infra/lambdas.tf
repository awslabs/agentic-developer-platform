# =============================================================================
# GitHub Webhook Lambda (Stub)
# =============================================================================
# Stub Lambda that returns 200. Actual HMAC validation + event processing
# will be implemented in issue #318.
# =============================================================================

data "archive_file" "webhook_lambda" {
  type        = "zip"
  output_path = "${path.module}/.build/github-webhook.zip"

  source {
    content  = <<-PYTHON
import json

def handler(event, context):
    """Stub webhook handler. Returns 200 to prove integration works.
    Actual logic (HMAC validation, tenant lookup, SQS publish) comes in #318.
    """
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"status": "ok", "message": "webhook-ingress stub"})
    }
PYTHON
    filename = "handler.py"
  }
}

resource "aws_lambda_function" "github_webhook" {
  function_name = "${local.name_prefix}-github-webhook"
  description   = "GitHub webhook ingress - validates and queues events"
  role          = aws_iam_role.lambda_execution.arn

  filename         = data.archive_file.webhook_lambda.output_path
  source_code_hash = data.archive_file.webhook_lambda.output_base64sha256
  handler          = "handler.handler"
  runtime          = var.lambda_runtime
  timeout          = var.lambda_timeout
  memory_size      = var.lambda_memory_size

  environment {
    variables = {
      ENVIRONMENT        = var.environment
      SQS_QUEUE_URL      = aws_sqs_queue.agent_submit.url
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
