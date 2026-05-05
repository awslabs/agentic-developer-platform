# =============================================================================
# Agent ScaledJob IAM Role (IRSA)
# =============================================================================
# Permissions for the hosted agent worker pods:
#   - SQS: receive + delete messages from agent-submit.fifo
#   - Bedrock: invoke models for agent reasoning
#   - Secrets Manager: read GitHub App keys, tenant credentials
#   - STS: assume customer AWS roles for operations-persona tasks
#
# Issue: #346
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
          AWS = data.aws_iam_role.keda_operator.arn
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

# --- SQS: receive + delete from submit queue ---

resource "aws_iam_role_policy" "agent_scaledjob_sqs" {
  name = "sqs-consume"
  role = aws_iam_role.agent_scaledjob.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "ConsumeSubmitQueue"
      Effect = "Allow"
      Action = [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes"
      ]
      Resource = aws_sqs_queue.agent_submit.arn
    }]
  })
}

# --- Bedrock: invoke models ---

resource "aws_iam_role_policy" "agent_scaledjob_bedrock" {
  name = "bedrock-invoke"
  role = aws_iam_role.agent_scaledjob.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "BedrockInvoke"
      Effect = "Allow"
      Action = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ]
      Resource = [
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:*:${local.account_id}:inference-profile/*"
      ]
    }]
  })
}

# --- Secrets Manager: read GitHub App keys + tenant secrets ---

resource "aws_iam_role_policy" "agent_scaledjob_secrets" {
  name = "secrets-read"
  role = aws_iam_role.agent_scaledjob.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "SecretsRead"
      Effect = "Allow"
      Action = [
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret"
      ]
      Resource = "arn:aws:secretsmanager:${var.aws_region}:${local.account_id}:secret:adp/*"
    }]
  })
}

# --- STS: assume customer AWS roles for operations persona ---

resource "aws_iam_role_policy" "agent_scaledjob_sts" {
  name = "sts-assume-role"
  role = aws_iam_role.agent_scaledjob.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "AssumeCustomerRoles"
      Effect   = "Allow"
      Action   = ["sts:AssumeRole", "sts:GetCallerIdentity"]
      Resource = "*"
      Condition = {
        StringEquals = {
          "sts:ExternalId" = "${local.name_prefix}-hosted-agent"
        }
      }
    }]
  })
}

# ---------------------------------------------------------------------------
# AgentCore Browser — for the url-analysis skill (EPIC #224 / #484)
# ---------------------------------------------------------------------------
# The hosted webhook agent image bundles cyber's url-analysis skill (via
# stage-personas.sh), so the developer/ops persona can run it directly
# without delegating to a cyber worker pod. Grant ephemeral browser-session
# permissions on the same role.
#
# Scoped to the current region via aws:RequestedRegion to prevent a
# misconfigured skill from spinning up sessions elsewhere (cost + audit
# containment).

resource "aws_iam_role_policy" "agent_scaledjob_agentcore_browser" {
  name = "agentcore-browser-access"
  role = aws_iam_role.agent_scaledjob.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "AgentCoreBrowserSessions"
      Effect = "Allow"
      Action = [
        "bedrock-agentcore:StartBrowserSession",
        "bedrock-agentcore:InvokeBrowser",
        "bedrock-agentcore:StopBrowserSession",
        "bedrock-agentcore:GetBrowserSession",
        "bedrock-agentcore:ListBrowserSessions",
        # Required for Playwright-over-CDP (skill's preferred path —
        # gives DOM/network/form access that InvokeBrowser lacks).
        # Without this the CDP WebSocket returns 403 "User is not
        # authorized to access automation stream".
        "bedrock-agentcore:ConnectBrowserAutomationStream",
        "bedrock-agentcore:UpdateBrowserStream",
      ]
      Resource = "*"
      Condition = {
        StringEquals = {
          "aws:RequestedRegion" = var.aws_region
        }
      }
    }]
  })
}
