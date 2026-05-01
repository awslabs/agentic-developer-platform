# =============================================================================
# Pre Sign-Up Lambda Trigger (Issue #314)
# =============================================================================
#
# This Lambda function is triggered by Cognito before a user is created.
# It gates sign-up for external provider (GitHub) users based on:
# - org mode: Check GitHub org membership
# - explicit mode: Check DynamoDB allowlist
# - open mode: Allow all
#

# Package the Lambda code
data "archive_file" "pre_signup" {
  type        = "zip"
  source_file = "${path.module}/lambda/pre_signup.py"
  output_path = "${path.module}/lambda/pre_signup.zip"
}

# IAM Role for the Lambda function
resource "aws_iam_role" "pre_signup" {
  name = "${var.name_prefix}-pre-signup-role"

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
    Name    = "${var.name_prefix}-pre-signup-role"
    Service = "iam"
    Purpose = "lambda-execution"
  })
}

# IAM Policy for Lambda to access CloudWatch Logs
resource "aws_iam_role_policy" "pre_signup_logs" {
  name = "${var.name_prefix}-pre-signup-logs"
  role = aws_iam_role.pre_signup.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.name_prefix}-pre-signup-trigger:*"
      }
    ]
  })
}

# IAM Policy for Lambda to read from DynamoDB allowlist table
resource "aws_iam_role_policy" "pre_signup_dynamodb" {
  name = "${var.name_prefix}-pre-signup-dynamodb"
  role = aws_iam_role.pre_signup.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:Query"
        ]
        Resource = [
          aws_dynamodb_table.signup_allowlist.arn,
          "${aws_dynamodb_table.signup_allowlist.arn}/index/*"
        ]
      }
    ]
  })
}

# IAM Policy for Lambda to read GitHub token from Secrets Manager
resource "aws_iam_role_policy" "pre_signup_secrets" {
  count = var.github_token_secret_arn != "" ? 1 : 0
  name  = "${var.name_prefix}-pre-signup-secrets"
  role  = aws_iam_role.pre_signup.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = [var.github_token_secret_arn]
      }
    ]
  })
}

# Lambda Function
resource "aws_lambda_function" "pre_signup" {
  function_name = "${var.name_prefix}-pre-signup-trigger"
  description   = "Cognito Pre Sign-Up trigger to gate GitHub user sign-ups"

  filename         = data.archive_file.pre_signup.output_path
  source_code_hash = data.archive_file.pre_signup.output_base64sha256

  handler = "pre_signup.handler"
  runtime = "python3.12"

  role = aws_iam_role.pre_signup.arn

  timeout     = 10 # seconds (GitHub API calls may take a few seconds)
  memory_size = 128

  environment {
    variables = {
      ALLOWLIST_MODE          = var.pre_signup_allowlist_mode
      ALLOWED_ORGS            = var.pre_signup_allowed_orgs
      ALLOWLIST_TABLE         = aws_dynamodb_table.signup_allowlist.name
      GITHUB_TOKEN_SECRET_ARN = var.github_token_secret_arn
      LOG_LEVEL               = var.environment == "prod" ? "INFO" : "DEBUG"
    }
  }

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-pre-signup-trigger"
    Service = "lambda"
    Purpose = "cognito-trigger"
  })
}

# CloudWatch Log Group for Lambda
resource "aws_cloudwatch_log_group" "pre_signup" {
  name              = "/aws/lambda/${var.name_prefix}-pre-signup-trigger"
  retention_in_days = var.environment == "prod" ? 30 : 7

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-pre-signup-trigger-logs"
    Service = "cloudwatch"
    Purpose = "lambda-logs"
  })
}

# Permission for Cognito to invoke the Lambda
resource "aws_lambda_permission" "cognito_pre_signup" {
  statement_id  = "AllowCognitoPreSignUpInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.pre_signup.function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.main.arn
}

# =============================================================================
# DynamoDB Table for Signup Allowlist (Issue #314)
# =============================================================================

resource "aws_dynamodb_table" "signup_allowlist" {
  name         = "${var.name_prefix}-signup-allowlist"
  billing_mode = "PAY_PER_REQUEST"

  # Primary key: username (GitHub username or email, lowercased)
  hash_key = "username"

  attribute {
    name = "username"
    type = "S"
  }

  # Enable point-in-time recovery in prod
  point_in_time_recovery {
    enabled = var.environment == "prod" ? true : false
  }

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-signup-allowlist"
    Service = "dynamodb"
    Purpose = "signup-access-control"
  })
}

# =============================================================================
# Outputs (Issue #314)
# =============================================================================

output "pre_signup_lambda_arn" {
  description = "ARN of the Pre Sign-Up Lambda function"
  value       = aws_lambda_function.pre_signup.arn
}

output "pre_signup_lambda_name" {
  description = "Name of the Pre Sign-Up Lambda function"
  value       = aws_lambda_function.pre_signup.function_name
}

output "signup_allowlist_table_name" {
  description = "Name of the DynamoDB signup allowlist table"
  value       = aws_dynamodb_table.signup_allowlist.name
}

output "signup_allowlist_table_arn" {
  description = "ARN of the DynamoDB signup allowlist table"
  value       = aws_dynamodb_table.signup_allowlist.arn
}
