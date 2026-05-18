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
      },
      {
        Sid    = "IdentityIndexReadWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:PutItem"
        ]
        Resource = var.identity_index_table_arn != "" ? [var.identity_index_table_arn] : ["arn:aws:dynamodb:${var.aws_region}:${local.account_id}:table/adp-${var.environment}-identity-index"]
      },
      {
        Sid    = "UserIdentityIndexRead"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem"
        ]
        Resource = "arn:aws:dynamodb:${var.aws_region}:${local.account_id}:table/adp-${var.environment}-user-identity-index"
      },
      {
        Sid    = "DynamoDBKMSAccess"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        # Two keys: this module's own KMS (encrypts tenant-registry,
        # webhook-events, rate-limits) plus the gateway's KMS (encrypts
        # identity-index + user-identity-index, which the Lambda READS to
        # resolve installation_id → tenant and sender_id → user). Without
        # the gateway key, every GetItem on those tables returns
        # AccessDeniedException and webhooks 403 with outcome=unknown_installation.
        Resource = [
          aws_kms_key.dynamodb.arn,
          data.aws_kms_alias.gateway_dynamodb.target_key_arn,
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_dynamodb" {
  role       = aws_iam_role.lambda_execution.name
  policy_arn = aws_iam_policy.lambda_dynamodb.arn
}

# Secrets Manager read/write
resource "aws_iam_policy" "lambda_secrets" {
  name        = "${local.name_prefix}-webhook-lambda-secrets"
  description = "Allow webhook Lambda to read webhook secret and manage per-tenant GitHub App secrets"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadWebhookSecret"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = aws_secretsmanager_secret.webhook_secret.arn
      },
      {
        Sid    = "ReadPlatformGitHubAppSecrets"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = [
          aws_secretsmanager_secret.github_app_id.arn,
          aws_secretsmanager_secret.github_app_key.arn,
        ]
      },
      {
        Sid      = "ReadInternalApiKey"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = var.internal_api_key_arn != "" ? [var.internal_api_key_arn] : []
      },
      {
        Sid    = "WritePerTenantGitHubAppSecrets"
        Effect = "Allow"
        Action = [
          "secretsmanager:CreateSecret",
          "secretsmanager:TagResource"
        ]
        Resource = "arn:aws:secretsmanager:${var.aws_region}:${local.account_id}:secret:adp/${var.environment}/tenants/*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_secrets" {
  role       = aws_iam_role.lambda_execution.name
  policy_arn = aws_iam_policy.lambda_secrets.arn
}

# CloudWatch PutMetricData — required by identity_resolver.py to emit
# IdentityResolver.CrossTenantMismatch on installation/user tenant disagreement
# (issue #537 follow-up; flagged by reviewer on PR #539).
#
# Narrow-scoped by namespace via the cloudwatch:namespace condition key so
# the Lambda can only publish under its own namespace, not overwrite others.
resource "aws_iam_policy" "lambda_cloudwatch_metrics" {
  name        = "${local.name_prefix}-webhook-lambda-cloudwatch-metrics"
  description = "Allow webhook Lambda to emit custom CloudWatch metrics under ADP/IdentityResolver"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "ADP/IdentityResolver"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_cloudwatch_metrics" {
  role       = aws_iam_role.lambda_execution.name
  policy_arn = aws_iam_policy.lambda_cloudwatch_metrics.arn
}
