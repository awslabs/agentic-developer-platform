# =============================================================================
# IAM Roles and Policies for Budget Lambda Functions (Issue #234)
# =============================================================================

# Get the current partition. Account and region are explicit module inputs so
# module-level dependencies cannot defer them to apply time.
data "aws_partition" "current" {}

# =============================================================================
# Usage Tracker Lambda IAM Role
# =============================================================================

resource "aws_iam_role" "usage_tracker" {
  permissions_boundary = var.automation_permissions_boundary_arn
  name                 = "${var.name_prefix}-budget-usage-tracker-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-budget-usage-tracker-role"
    Service = "lambda"
    Purpose = "budget-usage-tracker"
  })
}

# Usage Tracker Lambda Policy
resource "aws_iam_role_policy" "usage_tracker" {
  name = "${var.name_prefix}-budget-usage-tracker-policy"
  role = aws_iam_role.usage_tracker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # S3 Read Access for Chat Logs
      {
        Sid    = "S3ReadChatLogs"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:GetObjectVersion"
        ]
        Resource = "${var.chat_logs_bucket_arn}/*"
      },
      # RDS IAM Database Authentication
      {
        Sid    = "RDSConnect"
        Effect = "Allow"
        Action = [
          "rds-db:connect"
        ]
        Resource = var.rds_resource_id != "" ? "arn:aws:rds-db:${var.aws_region}:${var.account_id}:dbuser:${var.rds_resource_id}/${var.db_username}" : "arn:aws:rds-db:${var.aws_region}:${var.account_id}:dbuser:*/${var.db_username}"
      },
      # CloudWatch Logs
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/lambda/${var.name_prefix}-budget-usage-tracker:*"
      },
      # CloudWatch custom metrics (Issue #4592)
      #
      # pricing_fallback.get_model_pricing() publishes ADP/Gateway ·
      # UnknownModelPricing on every fallback-priced model, but this role never
      # had PutMetricData. The emit is wrapped in `except Exception: pass`, so
      # every publish failed silently — the log WARNING landed and the metric
      # never did. Without this the #4592 alarm can never fire.
      #
      # PutMetricData takes no resource-level permissions; scope it with the
      # namespace condition instead of leaving it fully open.
      {
        Sid    = "CloudWatchPutMetrics"
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "ADP/Gateway"
          }
        }
      },
      # VPC ENI Management
      {
        Sid    = "VPCExecution"
        Effect = "Allow"
        Action = [
          "ec2:CreateNetworkInterface",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DeleteNetworkInterface",
          "ec2:AssignPrivateIpAddresses",
          "ec2:UnassignPrivateIpAddresses"
        ]
        Resource = "*"
      }
    ]
  })
}

# =============================================================================
# Pricing Refresh Lambda IAM Role
# =============================================================================

resource "aws_iam_role" "pricing_refresh" {
  permissions_boundary = var.automation_permissions_boundary_arn
  name                 = "${var.name_prefix}-pricing-refresh-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-pricing-refresh-role"
    Service = "lambda"
    Purpose = "pricing-refresh"
  })
}

# Pricing Refresh Lambda Policy
resource "aws_iam_role_policy" "pricing_refresh" {
  name = "${var.name_prefix}-pricing-refresh-policy"
  role = aws_iam_role.pricing_refresh.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "PricingExecutionFailureDestination"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.pricing_execution_failure.arn
      },
      {
        Sid      = "PricingOperationalMetrics"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = { "cloudwatch:namespace" = "ADP/Gateway" }
        }
      },
      # AWS Pricing API Access (only available in us-east-1 and ap-south-1)
      {
        Sid    = "PricingAPIAccess"
        Effect = "Allow"
        Action = [
          "pricing:GetProducts",
          "pricing:DescribeServices",
          "pricing:GetAttributeValues"
        ]
        Resource = "*"
      },
      # RDS IAM Database Authentication
      {
        Sid    = "RDSConnect"
        Effect = "Allow"
        Action = [
          "rds-db:connect"
        ]
        Resource = var.rds_resource_id != "" ? "arn:aws:rds-db:${var.aws_region}:${var.account_id}:dbuser:${var.rds_resource_id}/${var.db_username}" : "arn:aws:rds-db:${var.aws_region}:${var.account_id}:dbuser:*/${var.db_username}"
      },
      # CloudWatch Logs
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/lambda/${var.name_prefix}-pricing-refresh:*"
      },
      # VPC ENI Management
      {
        Sid    = "VPCExecution"
        Effect = "Allow"
        Action = [
          "ec2:CreateNetworkInterface",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DeleteNetworkInterface",
          "ec2:AssignPrivateIpAddresses",
          "ec2:UnassignPrivateIpAddresses"
        ]
        Resource = "*"
      }
    ]
  })
}
