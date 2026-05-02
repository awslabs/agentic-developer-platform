# =============================================================================
# Pre Token Generation Lambda (Issue #119)
# =============================================================================
#
# This Lambda function is triggered by Cognito before issuing tokens.
# It injects custom claims (org_id, team_id, etc.) into access tokens for:
# - Human users: Copies custom attributes from user pool
# - Agents/Services: Looks up metadata from DynamoDB
#

# Package the Lambda code
data "archive_file" "pre_token_generation" {
  type        = "zip"
  source_file = "${path.module}/lambda/pre_token_generation.py"
  output_path = "${path.module}/lambda/pre_token_generation.zip"
}

# IAM Role for the Lambda function
resource "aws_iam_role" "pre_token_generation" {
  name = "${var.name_prefix}-pre-token-generation-role"

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
    Name    = "${var.name_prefix}-pre-token-generation-role"
    Service = "iam"
    Purpose = "lambda-execution"
  })
}

# IAM Policy for Lambda to access CloudWatch Logs
resource "aws_iam_role_policy" "pre_token_generation_logs" {
  name = "${var.name_prefix}-pre-token-generation-logs"
  role = aws_iam_role.pre_token_generation.id

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
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.name_prefix}-pre-token-generation:*"
      }
    ]
  })
}

# IAM Policy for Lambda to read from DynamoDB
resource "aws_iam_role_policy" "pre_token_generation_dynamodb" {
  name = "${var.name_prefix}-pre-token-generation-dynamodb"
  role = aws_iam_role.pre_token_generation.id

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
          aws_dynamodb_table.agent_clients.arn,
          "${aws_dynamodb_table.agent_clients.arn}/index/*"
        ]
      }
    ]
  })
}

# Lambda Function
resource "aws_lambda_function" "pre_token_generation" {
  function_name = "${var.name_prefix}-pre-token-generation"
  description   = "Cognito Pre Token Generation trigger to inject custom claims"

  filename         = data.archive_file.pre_token_generation.output_path
  source_code_hash = data.archive_file.pre_token_generation.output_base64sha256

  handler = "pre_token_generation.handler"
  runtime = "python3.12"

  role = aws_iam_role.pre_token_generation.arn

  timeout     = 5 # seconds
  memory_size = 128

  environment {
    variables = {
      AGENT_CLIENTS_TABLE = aws_dynamodb_table.agent_clients.name
      LOG_LEVEL           = var.environment == "prod" ? "INFO" : "DEBUG"
    }
  }

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-pre-token-generation"
    Service = "lambda"
    Purpose = "cognito-trigger"
  })
}

# CloudWatch Log Group for Lambda
resource "aws_cloudwatch_log_group" "pre_token_generation" {
  name              = "/aws/lambda/${var.name_prefix}-pre-token-generation"
  retention_in_days = var.environment == "prod" ? 30 : 7

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-pre-token-generation-logs"
    Service = "cloudwatch"
    Purpose = "lambda-logs"
  })
}

# Permission for Cognito to invoke the Lambda
resource "aws_lambda_permission" "cognito_pre_token" {
  statement_id  = "AllowCognitoInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.pre_token_generation.function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.main.arn
}

# Output Lambda ARN
output "pre_token_generation_lambda_arn" {
  description = "ARN of the Pre Token Generation Lambda function"
  value       = aws_lambda_function.pre_token_generation.arn
}
