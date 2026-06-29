# =============================================================================
# Agent ScaledJob IAM Role (IRSA) — Issue #1204 (synthesis #1200 Section 2)
# =============================================================================
# Scoped permissions for the hosted agent worker pods. Replaces the prior
# multi-inline-policy approach with a single consolidated inline policy
# matching the audit synthesis output.
#
# Permissions:
#   - SQS: receive + delete messages from agent-submit.fifo
#   - Bedrock: invoke models for agent reasoning
#   - Bedrock AgentCore: ephemeral browser sessions (url-analysis skill)
#   - Secrets Manager: read GitHub App keys, tenant credentials
#   - STS: assume customer AWS roles for operations-persona tasks
#   - Execute API: invoke internal gateway endpoints via SigV4
#   - DynamoDB: write correlation pointers
#   - CloudWatch Logs: agent execution logging
#   - S3: beads state + url-analysis evidence
#   - Preflight: read-only checks (multiple services)
#
# Issue: #346, #1204
# =============================================================================

resource "aws_iam_role" "agent_scaledjob" {
  name = "${local.name_prefix}-agent-scaledjob-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # IRSA: agent pods in adp-agents namespace assume this role
        Effect = "Allow"
        Principal = {
          Federated = local.oidc_provider_arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${replace(local.oidc_issuer, "https://", "")}:sub" = "system:serviceaccount:adp-agents:agent-scaledjob-sa"
            "${replace(local.oidc_issuer, "https://", "")}:aud" = "sts.amazonaws.com"
          }
        }
      },
      {
        # KEDA operator chain-assumes this role for SQS queue-depth polling
        Effect = "Allow"
        Principal = {
          AWS = aws_iam_role.keda_operator.arn
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name      = "${local.name_prefix}-agent-scaledjob-role"
    Component = "hosted-agent-worker"
  }
}

# =============================================================================
# Consolidated agent-worker policy (synthesis #1200 Section 2)
# =============================================================================
# Size: ~2.2 KB — well within the inline policy limit.
# =============================================================================

resource "aws_iam_role_policy" "agent_scaledjob_permissions" {
  name = "agent-worker-scoped-permissions"
  role = aws_iam_role.agent_scaledjob.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "BedrockModelInvoke"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream"
        ]
        Resource = [
          "arn:aws:bedrock:*:*:inference-profile/*",
          "arn:aws:bedrock:*::foundation-model/*"
        ]
      },
      {
        # Region restriction prevents a misconfigured skill from spinning up
        # browser sessions in other regions (cost + audit containment).
        Sid    = "BedrockAgentCoreBrowser"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:ConnectBrowserAutomationStream",
          "bedrock-agentcore:GetBrowserSession",
          "bedrock-agentcore:InvokeBrowser",
          "bedrock-agentcore:ListBrowserSessions",
          "bedrock-agentcore:StartBrowserSession",
          "bedrock-agentcore:StopBrowserSession",
          "bedrock-agentcore:UpdateBrowserStream"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:RequestedRegion" = var.aws_region
          }
        }
      },
      {
        Sid    = "DynamoDBTableMgmt"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem"
        ]
        Resource = "arn:aws:dynamodb:us-east-1:*:table/adp-*-correlation-pointers"
      },
      {
        Sid    = "DynamoDBWebhookEventsUpdate"
        Effect = "Allow"
        Action = [
          "dynamodb:UpdateItem"
        ]
        Resource = "arn:aws:dynamodb:us-east-1:*:table/adp-*-webhook-events"
      },
      {
        # The webhook-events (and correlation-pointers) tables are encrypted
        # with the customer-managed CMK aws_kms_key.dynamodb. Writing to a
        # CMK-encrypted table requires kms:Decrypt + kms:GenerateDataKey on the
        # key, not just the dynamodb action. Without this, the worker's
        # UpdateItem (lib/invocation_status.update_status) fails with
        # KMS AccessDeniedException, which is swallowed fail-soft, leaving the
        # invocation row frozen at webhook_received (issue #1455 Gate failure).
        # Mirrors the lambda role grant in iam.tf.
        Sid    = "DynamoDBKMSDecrypt"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = aws_kms_key.dynamodb.arn
      },
      {
        Sid    = "ExecuteAPIInvoke"
        Effect = "Allow"
        Action = [
          "execute-api:Invoke"
        ]
        Resource = "arn:aws:execute-api:us-east-1:*:*/*/*/agent/*"
      },
      {
        Sid    = "CloudWatchLogGroups"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:DescribeLogGroups",
          "logs:PutLogEvents"
        ]
        Resource = [
          "arn:aws:logs:us-east-1:*:log-group:/github-ccsdk-agent/logs",
          "arn:aws:logs:us-east-1:*:log-group:/github-ccsdk-agent/logs:*"
        ]
      },
      {
        Sid    = "Multiple"
        Effect = "Allow"
        Action = [
          "bedrock:ListFoundationModels",
          "codebuild:ListProjects",
          "cognito-idp:ListUserPools",
          "dynamodb:ListTables",
          "ecr:DescribeRepositories",
          "eks:ListClusters",
          "iam:GetUser",
          "iam:ListRoles",
          "s3:ListAllMyBuckets",
          "secretsmanager:ListSecrets"
        ]
        Resource = "*"
      },
      {
        Sid    = "S3Combined"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject"
        ]
        Resource = [
          "arn:aws:s3:::adp-*-agent-beads-state-*/*",
          "arn:aws:s3:::adp-*-url-analysis-evidence-v2-*/*"
        ]
      },
      {
        Sid    = "SecretsManagerOps"
        Effect = "Allow"
        Action = [
          "secretsmanager:DescribeSecret",
          "secretsmanager:GetSecretValue"
        ]
        Resource = "arn:aws:secretsmanager:us-east-1:*:secret:adp/*"
      },
      {
        Sid    = "SQSQueueMgmt"
        Effect = "Allow"
        Action = [
          "sqs:ChangeMessageVisibility",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:ReceiveMessage"
        ]
        Resource = "arn:aws:sqs:us-east-1:*:adp-*-agent-submit.fifo"
      },
      {
        # ExternalId condition prevents a compromised pod from assuming arbitrary
        # cross-account roles. Customer-vault roles must be configured to require
        # this ExternalId; without the condition, the agent could assume any role
        # that trusts the agent-worker principal in any account.
        Sid    = "STSIdentityAndAssume"
        Effect = "Allow"
        Action = [
          "sts:AssumeRole",
          "sts:GetCallerIdentity"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "sts:ExternalId" = "${local.name_prefix}-hosted-agent"
          }
        }
      }
    ]
  })
}
