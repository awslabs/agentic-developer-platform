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
  bucket = local.lambda_artifact_bucket
  key    = "lambda-artifacts/webhook-ingress/github.zip"
}

resource "aws_lambda_function" "github_webhook" {
  function_name                  = "${local.name_prefix}-github-webhook"
  description                    = "GitHub webhook ingress - validates and queues events"
  role                           = aws_iam_role.lambda_execution.arn
  reserved_concurrent_executions = var.enable_lambda_reserved_concurrency ? 20 : -1

  s3_bucket        = local.lambda_artifact_bucket
  s3_key           = "lambda-artifacts/webhook-ingress/github.zip"
  source_code_hash = data.aws_s3_object.github_lambda_zip.etag
  handler          = "handler.handler"
  runtime          = var.lambda_runtime
  timeout          = var.lambda_timeout
  memory_size      = var.lambda_memory_size

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      ENVIRONMENT                   = var.environment
      SUBMIT_QUEUE_URL              = aws_sqs_queue.agent_submit.url
      IDENTITY_INDEX_TABLE          = var.identity_index_table_name
      USER_IDENTITY_INDEX_TABLE     = "adp-${var.environment}-user-identity-index"
      EVENTS_TABLE                  = aws_dynamodb_table.webhook_events.name
      RATE_LIMITS_TABLE             = aws_dynamodb_table.rate_limits.name
      RATE_LIMIT_PER_WINDOW         = tostring(var.rate_limit_per_window)
      RATE_LIMIT_PER_HOUR           = tostring(var.rate_limit_per_hour)
      WEBHOOK_SECRET_ARN            = aws_secretsmanager_secret.webhook_secret.arn
      GATEWAY_API_URL               = var.gateway_api_url
      USER_IDENTITY_INDEX_V2_WRITE  = "false"
      USER_IDENTITY_INDEX_V2_READ   = "true"
      INTERNAL_API_KEY_ARN          = var.internal_api_key_arn
      RESOLVE_CANONICAL_VIA_GATEWAY = "true"
      CORRELATION_POINTERS_TABLE    = aws_dynamodb_table.correlation_pointers.name
      TENANT_REGISTRY_TABLE         = aws_dynamodb_table.tenant_registry.name
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
  kms_key_id        = aws_kms_key.cloudwatch.arn
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.github_webhook.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.webhook.execution_arn}/*/POST/github"
}

# Issue #2152: Lambda permission for POST /agent/trigger (AWS_IAM auth route)
resource "aws_lambda_permission" "api_gateway_agent_trigger" {
  statement_id  = "AllowAPIGatewayInvokeAgentTrigger"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.github_webhook.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.webhook.execution_arn}/*/POST/agent/trigger"
}
