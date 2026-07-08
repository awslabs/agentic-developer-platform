# =============================================================================
# GitLab Webhook Lambda (Issue #3324)
# =============================================================================
# Separate Lambda for GitLab webhook ingress. Validates X-Gitlab-Token header,
# parses note events for @agent mentions, and publishes to the shared
# agent-submit FIFO queue.
#
# Code source: the Package Lambda Code CI job uploads the zip to
#   s3://${lambda_artifact_bucket}/lambda-artifacts/webhook-ingress/gitlab.zip
# =============================================================================

data "aws_s3_object" "gitlab_lambda_zip" {
  count  = var.gitlab_webhook_enabled ? 1 : 0
  bucket = local.lambda_artifact_bucket
  key    = "lambda-artifacts/webhook-ingress/gitlab.zip"
}

resource "aws_lambda_function" "gitlab_webhook" {
  count         = var.gitlab_webhook_enabled ? 1 : 0
  function_name = "${local.name_prefix}-gitlab-webhook"
  description   = "GitLab webhook ingress - validates token and queues mention events"
  role          = aws_iam_role.lambda_execution.arn

  s3_bucket        = local.lambda_artifact_bucket
  s3_key           = "lambda-artifacts/webhook-ingress/gitlab.zip"
  source_code_hash = var.gitlab_webhook_enabled ? data.aws_s3_object.gitlab_lambda_zip[0].etag : ""
  handler          = "handler.handler"
  runtime          = var.lambda_runtime
  timeout          = var.lambda_timeout
  memory_size      = var.lambda_memory_size

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      ENVIRONMENT               = var.environment
      SUBMIT_QUEUE_URL          = aws_sqs_queue.agent_submit.url
      GITLAB_WEBHOOK_SECRET_ARN = aws_secretsmanager_secret.gitlab_webhook_secret[0].arn
    }
  }

  depends_on = [aws_cloudwatch_log_group.gitlab_lambda[0]]
}

resource "aws_cloudwatch_log_group" "gitlab_lambda" {
  count             = var.gitlab_webhook_enabled ? 1 : 0
  name              = "/aws/lambda/${local.name_prefix}-gitlab-webhook"
  retention_in_days = 14
  kms_key_id        = aws_kms_key.cloudwatch.arn
}

resource "aws_lambda_permission" "api_gateway_gitlab" {
  count         = var.gitlab_webhook_enabled ? 1 : 0
  statement_id  = "AllowAPIGatewayInvokeGitLab"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.gitlab_webhook[0].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.webhook.execution_arn}/*/POST/gitlab"
}
