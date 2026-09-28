# =============================================================================
# Budget Lambda Module (Issue #234)
# =============================================================================
# Creates two Lambda functions for accurate budget tracking:
# 1. Usage Tracker Lambda - S3 event-driven cost recording from chat logs
# 2. Pricing Refresh Lambda - Daily pricing updates from AWS Pricing API
# =============================================================================

# =============================================================================
# Shared Security Group for Lambda Functions
# =============================================================================

resource "aws_security_group" "lambda" {
  name        = "${var.name_prefix}-budget-lambda-sg"
  description = "Security group for budget tracking Lambda functions"
  vpc_id      = var.vpc_id

  # Allow outbound to RDS on port 5432
  egress {
    description     = "PostgreSQL to RDS"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [var.rds_security_group_id]
  }

  # Allow outbound HTTPS for AWS API calls (Pricing API, S3, etc.)
  egress {
    description = "HTTPS for AWS APIs"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-budget-lambda-sg"
    Service = "lambda"
    Purpose = "budget-tracking"
  })
}

# Add ingress rule to RDS security group to allow Lambda access
resource "aws_security_group_rule" "lambda_to_rds" {
  description              = "Allow Budget Lambda functions to access RDS PostgreSQL"
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = var.rds_security_group_id
  source_security_group_id = aws_security_group.lambda.id
}

# =============================================================================
# Lambda Deployment Package
# =============================================================================

# Issue #4969: the shared pricing policy package, vendored into BOTH Lambda zips.
#
# Enumerated with fileset() rather than one source block per file on purpose.
# The hand-listed pattern below it has already shipped a Lambda that ImportErrors
# on cold start when a module was added and not listed (see #4391), and this
# package is worse for that failure mode: it carries snapshots/*.json data files
# whose absence produces a FileNotFoundError only when a rate is actually looked
# up. Globbing means adding a snapshot or a module cannot desync the archives.
#
# Both Lambdas and the gateway image must carry the same package: it is the single
# source of pricing truth, and a version skew between the estimator and the
# settlement path is the class of bug #4969 exists to remove.
locals {
  pricing_policy_dir = "${path.root}/../pricing_policy"
  pricing_policy_files = concat(
    tolist(fileset(local.pricing_policy_dir, "**/*.py")),
    tolist(fileset(local.pricing_policy_dir, "snapshots/*.json")),
  )
  shared_lambda_dir   = "${path.root}/../lambda/shared"
  shared_lambda_files = fileset(local.shared_lambda_dir, "*.py")
}

# Archive the usage tracker Lambda code
data "archive_file" "usage_tracker" {
  type        = "zip"
  output_path = "${path.module}/usage_tracker.zip"

  dynamic "source" {
    for_each = fileset("${path.root}/../lambda/budget-usage-tracker", "*.py")
    content {
      content  = file("${path.root}/../lambda/budget-usage-tracker/${source.value}")
      filename = source.value
    }
  }

  dynamic "source" {
    for_each = local.pricing_policy_files
    content {
      content  = file("${local.pricing_policy_dir}/${source.value}")
      filename = "pricing_policy/${source.value}"
    }
  }

  dynamic "source" {
    for_each = local.shared_lambda_files
    content {
      content  = file("${local.shared_lambda_dir}/${source.value}")
      filename = source.value
    }
  }
}

# Archive the pricing refresh Lambda code
data "archive_file" "pricing_refresh" {
  type        = "zip"
  output_path = "${path.module}/pricing_refresh.zip"

  dynamic "source" {
    for_each = fileset("${path.root}/../lambda/pricing-refresh", "*.py")
    content {
      content  = file("${path.root}/../lambda/pricing-refresh/${source.value}")
      filename = source.value
    }
  }

  dynamic "source" {
    for_each = local.pricing_policy_files
    content {
      content  = file("${local.pricing_policy_dir}/${source.value}")
      filename = "pricing_policy/${source.value}"
    }
  }

  dynamic "source" {
    for_each = local.shared_lambda_files
    content {
      content  = file("${local.shared_lambda_dir}/${source.value}")
      filename = source.value
    }
  }
}

# =============================================================================
# Lambda Layer for psycopg2 (S3-sourced — Issue #1038)
# =============================================================================
# The layer zip is built by CodeBuild (adp-dev-psycopg2-layer project) and
# uploaded to S3. Terraform references it via data source — no Docker daemon
# required at apply time.
# =============================================================================

data "aws_s3_object" "psycopg2_layer" {
  bucket = var.lambda_artifact_bucket
  key    = "lambda-layers/psycopg2-py312.zip"
}

resource "aws_lambda_layer_version" "psycopg2" {
  layer_name          = "${var.name_prefix}-psycopg2-py312"
  description         = "psycopg2-binary 2.9.9 for Python 3.12 (x86_64)"
  s3_bucket           = var.lambda_artifact_bucket
  s3_key              = "lambda-layers/psycopg2-py312.zip"
  source_code_hash    = data.aws_s3_object.psycopg2_layer.etag
  compatible_runtimes = ["python3.12"]

  compatible_architectures = ["x86_64"]

  lifecycle {
    create_before_destroy = true
    # Layer content is managed by CodeBuild workflow; don't replace on etag drift
    # during plan if the zip hasn't actually changed semantically.
    ignore_changes = [source_code_hash]
  }
}

# =============================================================================
# Usage Tracker Lambda Function
# =============================================================================

resource "aws_lambda_function" "usage_tracker" {
  function_name                  = "${var.name_prefix}-budget-usage-tracker"
  description                    = "Tracks budget usage from S3 chat logs (Issue #234)"
  reserved_concurrent_executions = var.enable_reserved_concurrency ? 5 : -1

  filename         = data.archive_file.usage_tracker.output_path
  source_code_hash = data.archive_file.usage_tracker.output_base64sha256

  handler     = "handler.handler"
  runtime     = "python3.12"
  memory_size = var.usage_tracker_memory
  timeout     = var.usage_tracker_timeout

  role   = aws_iam_role.usage_tracker.arn
  layers = [aws_lambda_layer_version.psycopg2.arn]

  tracing_config {
    mode = "Active"
  }

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      DB_HOST     = var.db_host
      DB_PORT     = tostring(var.db_port)
      DB_NAME     = var.db_name
      DB_USERNAME = var.db_username
    }
  }

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-budget-usage-tracker"
    Service = "lambda"
    Purpose = "budget-usage-tracker"
  })

  depends_on = [
    aws_cloudwatch_log_group.usage_tracker,
    aws_security_group_rule.lambda_to_rds,
  ]
}

# CloudWatch Log Group for Usage Tracker
resource "aws_cloudwatch_log_group" "usage_tracker" {
  #checkov:skip=CKV_AWS_338: Budget Lambda logs use an explicitly bounded operational retention below the one-year audit-log policy.
  name              = "/aws/lambda/${var.name_prefix}-budget-usage-tracker"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.cloudwatch_kms_key_arn

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-budget-usage-tracker-logs"
    Service = "cloudwatch"
    Purpose = "budget-usage-tracker"
  })
}

# S3 Event Permission for Usage Tracker Lambda
#
# source_account is REQUIRED here, not defence in depth. An S3 bucket ARN carries
# no account id — `arn:aws:s3:::name` is globally namespaced — so source_arn alone
# does not bind this permission to our account. If the chat-logs bucket were ever
# deleted, anyone could create a bucket with the same name in their own account and
# its notifications would satisfy our source_arn, invoking this function with
# attacker-controlled objects. That is the confused-deputy case, and it is why AWS
# requires both conditions for the S3 principal specifically.
#
# Every other aws_lambda_permission in this repo is already sufficient with
# source_arn alone, because execute-api, cognito-idp, events and logs ARNs all embed
# the account id. Do not "fix" those to match this one.
resource "aws_lambda_permission" "usage_tracker_s3" {
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.usage_tracker.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = var.chat_logs_bucket_arn
  # Use the root module's already-resolved identity. A module-level depends_on
  # defers data sources inside this module, and an unknown source_account is a
  # force-new diff that would briefly remove this confused-deputy protection.
  source_account = var.account_id
}

# S3 Bucket Notification for Usage Tracker
resource "aws_s3_bucket_notification" "chat_logs" {
  bucket = var.chat_logs_bucket_name

  lambda_function {
    lambda_function_arn = aws_lambda_function.usage_tracker.arn
    events              = ["s3:ObjectCreated:*"]
    filter_suffix       = ".json"
  }

  depends_on = [aws_lambda_permission.usage_tracker_s3]
}

# =============================================================================
# Pricing Refresh Lambda Function
# =============================================================================

resource "aws_lambda_function" "pricing_refresh" {
  function_name                  = "${var.name_prefix}-pricing-refresh"
  description                    = "Publishes validated AWS Bedrock pricing generations from AWS catalogs and model cards"
  reserved_concurrent_executions = var.enable_reserved_concurrency ? 2 : -1

  filename         = data.archive_file.pricing_refresh.output_path
  source_code_hash = data.archive_file.pricing_refresh.output_base64sha256

  handler     = "handler.handler"
  runtime     = "python3.12"
  memory_size = var.pricing_refresh_memory
  timeout     = var.pricing_refresh_timeout

  role   = aws_iam_role.pricing_refresh.arn
  layers = [aws_lambda_layer_version.psycopg2.arn]

  tracing_config {
    mode = "Active"
  }

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      DB_HOST     = var.db_host
      DB_PORT     = tostring(var.db_port)
      DB_NAME     = var.db_name
      DB_USERNAME = var.db_username
    }
  }

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-pricing-refresh"
    Service = "lambda"
    Purpose = "pricing-refresh"
  })

  depends_on = [
    aws_cloudwatch_log_group.pricing_refresh,
    aws_security_group_rule.lambda_to_rds,
  ]
}

# CloudWatch Log Group for Pricing Refresh
resource "aws_cloudwatch_log_group" "pricing_refresh" {
  #checkov:skip=CKV_AWS_338: Pricing Lambda logs use an explicitly bounded operational retention below the one-year audit-log policy.
  name              = "/aws/lambda/${var.name_prefix}-pricing-refresh"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.cloudwatch_kms_key_arn

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-pricing-refresh-logs"
    Service = "cloudwatch"
    Purpose = "pricing-refresh"
  })
}

# EventBridge Schedule Rule for Daily Pricing Refresh
resource "aws_cloudwatch_event_rule" "pricing_refresh" {
  name                = "${var.name_prefix}-pricing-refresh-schedule"
  description         = "Triggers pricing refresh Lambda daily"
  schedule_expression = var.pricing_refresh_schedule
  # Creation and corrective infra applies must not start publication before the
  # matching code/schema are ready. The release verifier explicitly enables the
  # rule after seed, code, notification and immediate-refresh checks pass.
  state = "DISABLED"

  lifecycle {
    ignore_changes = [state]
  }
}

# EventBridge Target for Pricing Refresh Lambda
resource "aws_cloudwatch_event_target" "pricing_refresh" {
  rule      = aws_cloudwatch_event_rule.pricing_refresh.name
  target_id = "${var.name_prefix}-pricing-refresh"
  arn       = aws_lambda_function.pricing_refresh.arn

  retry_policy {
    maximum_retry_attempts       = 2
    maximum_event_age_in_seconds = 3600
  }

  dead_letter_config {
    arn = aws_sqs_queue.pricing_delivery_failure.arn
  }

  depends_on = [aws_sqs_queue_policy.pricing_delivery_failure, aws_lambda_permission.pricing_refresh_eventbridge]
}

# EventBridge Permission for Pricing Refresh Lambda
resource "aws_lambda_permission" "pricing_refresh_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.pricing_refresh.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.pricing_refresh.arn
}
