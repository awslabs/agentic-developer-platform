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

# Archive the usage tracker Lambda code
data "archive_file" "usage_tracker" {
  type        = "zip"
  output_path = "${path.module}/usage_tracker.zip"

  source {
    content  = file("${path.root}/../lambda/budget-usage-tracker/handler.py")
    filename = "handler.py"
  }

  source {
    content  = file("${path.root}/../lambda/shared/db.py")
    filename = "db.py"
  }

  source {
    content  = file("${path.root}/../lambda/shared/pricing_fallback.py")
    filename = "pricing_fallback.py"
  }
}

# Archive the pricing refresh Lambda code
data "archive_file" "pricing_refresh" {
  type        = "zip"
  output_path = "${path.module}/pricing_refresh.zip"

  source {
    content  = file("${path.root}/../lambda/pricing-refresh/handler.py")
    filename = "handler.py"
  }

  source {
    content  = file("${path.root}/../lambda/shared/db.py")
    filename = "db.py"
  }

  source {
    content  = file("${path.root}/../lambda/shared/pricing_fallback.py")
    filename = "pricing_fallback.py"
  }
}

# =============================================================================
# Lambda Layer for psycopg2
# =============================================================================
# Builds psycopg2-binary into a Lambda Layer. The build script auto-detects
# the platform: on Linux x86_64 (CI runners) it uses pip directly; on
# macOS/ARM it uses a container runtime (finch/docker).
# =============================================================================

resource "null_resource" "psycopg2_layer_build" {
  # Always rebuild — the python/ directory doesn't persist between CI runs
  triggers = {
    always_run = timestamp()
  }

  provisioner "local-exec" {
    command     = "bash build.sh"
    working_dir = "${path.root}/../lambda/layers/psycopg2"
  }
}

data "archive_file" "psycopg2_layer" {
  type        = "zip"
  source_dir  = "${path.root}/../lambda/layers/psycopg2"
  output_path = "${path.module}/psycopg2_layer.zip"
  excludes    = ["build.sh"]

  depends_on = [null_resource.psycopg2_layer_build]
}

resource "aws_lambda_layer_version" "psycopg2" {
  layer_name          = "${var.name_prefix}-psycopg2-py312"
  description         = "psycopg2-binary 2.9.9 for Python 3.12 (x86_64)"
  filename            = data.archive_file.psycopg2_layer.output_path
  source_code_hash    = data.archive_file.psycopg2_layer.output_base64sha256
  compatible_runtimes = ["python3.12"]

  lifecycle {
    create_before_destroy = true
  }
}

# =============================================================================
# Usage Tracker Lambda Function
# =============================================================================

resource "aws_lambda_function" "usage_tracker" {
  function_name = "${var.name_prefix}-budget-usage-tracker"
  description   = "Tracks budget usage from S3 chat logs (Issue #234)"

  filename         = data.archive_file.usage_tracker.output_path
  source_code_hash = data.archive_file.usage_tracker.output_base64sha256

  handler     = "handler.handler"
  runtime     = "python3.12"
  memory_size = var.usage_tracker_memory
  timeout     = var.usage_tracker_timeout

  role   = aws_iam_role.usage_tracker.arn
  layers = [aws_lambda_layer_version.psycopg2.arn]

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
  name              = "/aws/lambda/${var.name_prefix}-budget-usage-tracker"
  retention_in_days = var.log_retention_days

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-budget-usage-tracker-logs"
    Service = "cloudwatch"
    Purpose = "budget-usage-tracker"
  })
}

# S3 Event Permission for Usage Tracker Lambda
resource "aws_lambda_permission" "usage_tracker_s3" {
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.usage_tracker.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = var.chat_logs_bucket_arn
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
  function_name = "${var.name_prefix}-pricing-refresh"
  description   = "Refreshes model pricing from AWS Pricing API (Issue #234)"

  filename         = data.archive_file.pricing_refresh.output_path
  source_code_hash = data.archive_file.pricing_refresh.output_base64sha256

  handler     = "handler.handler"
  runtime     = "python3.12"
  memory_size = var.pricing_refresh_memory
  timeout     = var.pricing_refresh_timeout

  role   = aws_iam_role.pricing_refresh.arn
  layers = [aws_lambda_layer_version.psycopg2.arn]

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
  name              = "/aws/lambda/${var.name_prefix}-pricing-refresh"
  retention_in_days = var.log_retention_days

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
}

# EventBridge Target for Pricing Refresh Lambda
resource "aws_cloudwatch_event_target" "pricing_refresh" {
  rule      = aws_cloudwatch_event_rule.pricing_refresh.name
  target_id = "${var.name_prefix}-pricing-refresh"
  arn       = aws_lambda_function.pricing_refresh.arn
}

# EventBridge Permission for Pricing Refresh Lambda
resource "aws_lambda_permission" "pricing_refresh_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.pricing_refresh.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.pricing_refresh.arn
}
