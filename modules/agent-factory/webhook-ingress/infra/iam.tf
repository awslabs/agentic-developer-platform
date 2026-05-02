# =============================================================================
# IAM — Lambda Execution Role
# =============================================================================
# Grants the webhook Lambda: CloudWatch Logs, SQS SendMessage, DDB access,
# and Secrets Manager read for the webhook secret.
# =============================================================================

resource "aws_iam_role" "lambda_execution" {
  name = "${local.name_prefix}-webhook-lambda-role"

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
}

# CloudWatch Logs
resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# SQS SendMessage
resource "aws_iam_policy" "lambda_sqs" {
  name        = "${local.name_prefix}-webhook-lambda-sqs"
  description = "Allow webhook Lambda to send messages to agent-submit FIFO queue"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:GetQueueUrl",
          "sqs:GetQueueAttributes"
        ]
        Resource = aws_sqs_queue.agent_submit.arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_sqs" {
  role       = aws_iam_role.lambda_execution.name
  policy_arn = aws_iam_policy.lambda_sqs.arn
}

# DynamoDB access
resource "aws_iam_policy" "lambda_dynamodb" {
  name        = "${local.name_prefix}-webhook-lambda-ddb"
  description = "Allow webhook Lambda to read/write DynamoDB tables"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:UpdateItem"
        ]
        Resource = [
          aws_dynamodb_table.tenant_registry.arn,
          aws_dynamodb_table.webhook_events.arn,
          "${aws_dynamodb_table.webhook_events.arn}/index/*",
          aws_dynamodb_table.rate_limits.arn,
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_dynamodb" {
  role       = aws_iam_role.lambda_execution.name
  policy_arn = aws_iam_policy.lambda_dynamodb.arn
}

# Secrets Manager read
resource "aws_iam_policy" "lambda_secrets" {
  name        = "${local.name_prefix}-webhook-lambda-secrets"
  description = "Allow webhook Lambda to read webhook secret from Secrets Manager"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = aws_secretsmanager_secret.webhook_secret.arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_secrets" {
  role       = aws_iam_role.lambda_execution.name
  policy_arn = aws_iam_policy.lambda_secrets.arn
}
