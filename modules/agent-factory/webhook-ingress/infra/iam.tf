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
        Sid    = "CorrelationPointersReadWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem"
        ]
        Resource = aws_dynamodb_table.correlation_pointers.arn
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
    Statement = concat(
      [
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
          Sid    = "WritePerTenantGitHubAppSecrets"
          Effect = "Allow"
          Action = [
            "secretsmanager:CreateSecret",
            "secretsmanager:TagResource"
          ]
          Resource = "arn:aws:secretsmanager:${var.aws_region}:${local.account_id}:secret:adp/${var.environment}/tenants/*"
        },
        {
          # Issue #2567: The webhook secret, GitHub App ID, and GitHub App key
          # are encrypted with aws_kms_key.secrets (CMK). Without kms:Decrypt
          # on this key, GetSecretValue returns AccessDeniedException even though
          # the secretsmanager:GetSecretValue permission is granted above.
          Sid    = "SecretsKMSDecrypt"
          Effect = "Allow"
          Action = [
            "kms:Decrypt",
            "kms:DescribeKey"
          ]
          Resource = aws_kms_key.secrets.arn
        }
      ],
      var.internal_api_key_arn != "" ? [
        {
          Sid      = "ReadInternalApiKey"
          Effect   = "Allow"
          Action   = ["secretsmanager:GetSecretValue"]
          Resource = [var.internal_api_key_arn]
        }
      ] : []
    )
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
  description = "Allow webhook Lambda to emit custom CloudWatch metrics under ADP/IdentityResolver and WebhookIngress"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = ["ADP/IdentityResolver", "WebhookIngress"]
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

# =============================================================================
# IAM — Gateway Activity read path (issue #2754)
# =============================================================================
# The gateway's /api/admin/agent-invocations and /api/me/agent-invocations
# endpoints Query the webhook-events table (incl. tenant-index) and must decrypt
# its SSE-KMS key. Both the table and the key are defined in this stack, so we
# reference them as in-state ARNs (scoped — no table/* or kms:* wildcards).
#
# The gateway role (adp-${var.environment}-role-gateway-service) is created by
# platform/infra (platform/infra/modules/eks/main.tf -> aws_iam_role
# .gateway_service_irsa), which always applies before this stack, so attaching
# an inline policy to it by name is safe.
#
# Single combined policy named to match the platform account's original
# hand-patch (adp-dev-policy-gateway-activity-read): PutRolePolicy is an upsert,
# so this converges on that account without an import. The account's second
# hand-patched policy (-activity-kms) becomes orphaned and is deleted manually
# post-apply (see issue #2754 Deployment section).
resource "aws_iam_role_policy" "gateway_activity_read" {
  name = "adp-${var.environment}-policy-gateway-activity-read"
  role = "adp-${var.environment}-role-gateway-service"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "WebhookEventsRead"
        Effect = "Allow"
        Action = [
          "dynamodb:Query",
          "dynamodb:GetItem",
        ]
        Resource = [
          aws_dynamodb_table.webhook_events.arn,
          "${aws_dynamodb_table.webhook_events.arn}/index/*",
        ]
      },
      {
        Sid    = "WebhookEventsKMSDecrypt"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
        ]
        Resource = [aws_kms_key.dynamodb.arn]
      }
    ]
  })
}
