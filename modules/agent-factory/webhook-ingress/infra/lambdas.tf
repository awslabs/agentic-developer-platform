# =============================================================================
# GitHub Webhook Lambda
# =============================================================================
# Full handler: HMAC validation via Secrets Manager, tenant resolution,
# rate limiting, intent parsing, and SQS publish.
#
# Code source: the Package Lambda Code CI job uploads the zip to
#   s3://${lambda_artifact_bucket}/lambda-artifacts/webhook-ingress/github.zip
# Terraform reads the zip's SHA from S3 object metadata, so apply tracks
# code version without needing the zip on the local filesystem. The
# Update Lambda Function Code CI job is the authoritative code publisher
# on each deploy — Terraform only manages config.
# =============================================================================

data "aws_s3_object" "github_lambda_zip" {
  bucket = var.lambda_artifact_bucket
  key    = "lambda-artifacts/webhook-ingress/github.zip"
}

resource "aws_lambda_function" "github_webhook" {
  function_name = "${local.name_prefix}-github-webhook"
  description   = "GitHub webhook ingress - validates and queues events"
  role          = aws_iam_role.lambda_execution.arn

  s3_bucket         = var.lambda_artifact_bucket
  s3_key            = "lambda-artifacts/webhook-ingress/github.zip"
  source_code_hash  = data.aws_s3_object.github_lambda_zip.etag
  handler           = "handler.handler"
  runtime           = var.lambda_runtime
  timeout           = var.lambda_timeout
  memory_size       = var.lambda_memory_size

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

  # Terraform reads source_code_hash from the S3 object etag (see data
  # source above). When CI uploads a new zip to the same S3 key, the etag
  # changes and the next Terraform apply will pick it up automatically.
  # The separate Update Lambda Function Code CI job is still the fast-path
  # code publisher (no full TF apply needed for a code-only change); its
  # updates are idempotent with Terraform's view because they write to the
  # same S3 source.

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
