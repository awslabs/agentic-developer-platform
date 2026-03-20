# =============================================================================
# IAM Roles and Policies for Budget Lambda Functions (Issue #234)
# =============================================================================

# Get current AWS account ID and region
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# =============================================================================
# Usage Tracker Lambda IAM Role
# =============================================================================

resource "aws_iam_role" "usage_tracker" {
  name = "${var.name_prefix}-budget-usage-tracker-role"

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
        Resource = var.rds_resource_id != "" ? "arn:aws:rds-db:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:dbuser:${var.rds_resource_id}/${var.db_username}" : "arn:aws:rds-db:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:dbuser:*/${var.db_username}"
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
        Resource = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.name_prefix}-budget-usage-tracker:*"
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
  name = "${var.name_prefix}-pricing-refresh-role"

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
        Resource = var.rds_resource_id != "" ? "arn:aws:rds-db:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:dbuser:${var.rds_resource_id}/${var.db_username}" : "arn:aws:rds-db:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:dbuser:*/${var.db_username}"
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
        Resource = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.name_prefix}-pricing-refresh:*"
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
