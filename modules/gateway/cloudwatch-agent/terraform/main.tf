# CloudWatch Agent: Triggers GitHub issues from CloudWatch log errors
#
# This module creates:
# - Lambda function to process log errors
# - DynamoDB table for deduplication
# - IAM roles and policies
# - CloudWatch subscription filters (optional, can be added separately)

terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

# =============================================================================
# Variables
# =============================================================================

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name prefix for resources"
  type        = string
  default     = "cloudwatch-agent"
}

variable "github_org" {
  description = "GitHub organization name"
  type        = string
}

variable "github_secret_name" {
  description = "Secrets Manager secret name containing GitHub PAT"
  type        = string
  default     = "github-ccsdk-agent/github-pat"
}

variable "default_repo" {
  description = "Default GitHub repo if log group has no mapping"
  type        = string
  default     = ""
}

variable "cooldown_seconds" {
  description = "Minimum seconds between issues for same error"
  type        = number
  default     = 300
}

variable "log_group_repo_map" {
  description = "Map of log group names/patterns to GitHub repo names"
  type        = map(string)
  default     = {}
}

variable "log_groups" {
  description = "List of log groups to subscribe to (creates subscription filters)"
  type = list(object({
    name           = string
    filter_pattern = optional(string, "?ERROR ?Exception ?FATAL ?CRITICAL")
  }))
  default = []
}

variable "error_filter_pattern" {
  description = "Default CloudWatch Logs filter pattern for errors"
  type        = string
  default     = "?ERROR ?Exception ?FATAL ?CRITICAL"
}

# =============================================================================
# Data Sources
# =============================================================================

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# =============================================================================
# DynamoDB Table for Deduplication
# =============================================================================

resource "aws_dynamodb_table" "dedup" {
  name         = "${var.project_name}-dedup"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "error_hash"
  range_key    = "repo"

  attribute {
    name = "error_hash"
    type = "S"
  }

  attribute {
    name = "repo"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.dynamodb.arn
  }

  tags = {
    Name    = "${var.project_name}-dedup"
    Project = var.project_name
  }
}

# =============================================================================
# IAM Role for Lambda
# =============================================================================

resource "aws_iam_role" "lambda" {
  name = "${var.project_name}-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "${var.project_name}-lambda-policy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:GetLogEvents",
          "logs:DescribeLogStreams",
          "logs:ListTagsForResource"
        ]
        Resource = "*"
      },
      {
        Sid    = "SecretsManager"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:${var.github_secret_name}*"
      },
      {
        Sid    = "DynamoDB"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem"
        ]
        Resource = aws_dynamodb_table.dedup.arn
      },
      {
        Sid    = "DynamoDBKMSAccess"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = [aws_kms_key.dynamodb.arn]
      },
      {
        Sid    = "STS"
        Effect = "Allow"
        Action = [
          "sts:GetCallerIdentity"
        ]
        Resource = "*"
      }
    ]
  })
}

# =============================================================================
# Lambda Function
# =============================================================================

data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda"
  output_path = "${path.module}/lambda.zip"
}

resource "aws_lambda_function" "main" {
  function_name    = var.project_name
  role             = aws_iam_role.lambda.arn
  handler          = "handler.handler"
  runtime          = "python3.11"
  timeout          = 60
  memory_size      = 256
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  environment {
    variables = {
      GITHUB_SECRET_NAME = var.github_secret_name
      GITHUB_ORG         = var.github_org
      DEFAULT_REPO       = var.default_repo
      COOLDOWN_SECONDS   = tostring(var.cooldown_seconds)
      DEDUP_TABLE        = aws_dynamodb_table.dedup.name
      AWS_REGION         = data.aws_region.current.name
      LOG_GROUP_REPO_MAP = jsonencode(var.log_group_repo_map)
    }
  }

  tags = {
    Name    = var.project_name
    Project = var.project_name
  }
}

# Lambda permission for CloudWatch Logs
resource "aws_lambda_permission" "cloudwatch" {
  statement_id  = "AllowCloudWatchLogs"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.main.function_name
  principal     = "logs.amazonaws.com"
  source_arn    = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:*"
}

# =============================================================================
# CloudWatch Subscription Filters (Optional)
# =============================================================================

resource "aws_cloudwatch_log_subscription_filter" "main" {
  for_each = { for lg in var.log_groups : lg.name => lg }

  name            = "${var.project_name}-${replace(each.key, "/", "-")}"
  log_group_name  = each.key
  filter_pattern  = each.value.filter_pattern
  destination_arn = aws_lambda_function.main.arn

  depends_on = [aws_lambda_permission.cloudwatch]
}

# =============================================================================
# Outputs
# =============================================================================

output "lambda_function_arn" {
  description = "ARN of the Lambda function"
  value       = aws_lambda_function.main.arn
}

output "lambda_function_name" {
  description = "Name of the Lambda function"
  value       = aws_lambda_function.main.function_name
}

output "dedup_table_name" {
  description = "Name of the DynamoDB deduplication table"
  value       = aws_dynamodb_table.dedup.name
}

output "add_subscription_command" {
  description = "Command to add a subscription filter to a log group"
  value       = <<-EOT
    aws logs put-subscription-filter \
      --log-group-name <LOG_GROUP_NAME> \
      --filter-name "${var.project_name}-filter" \
      --filter-pattern "?ERROR ?Exception ?FATAL" \
      --destination-arn ${aws_lambda_function.main.arn}
  EOT
}
