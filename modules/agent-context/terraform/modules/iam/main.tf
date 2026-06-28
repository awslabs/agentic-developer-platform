# =============================================================================
# Agent Context Platform — IRSA Role
# =============================================================================
# Creates the IAM role used by the agent-context Kubernetes service account.
# Trust is scoped to system:serviceaccount:<namespace>:<service_account> on
# the platform's OIDC provider.
# =============================================================================

data "aws_partition" "current" {}

locals {
  partition = data.aws_partition.current.partition
}

# --- Trust Policy (IRSA) ---

data "aws_iam_policy_document" "assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_issuer}:sub"
      values   = ["system:serviceaccount:${var.namespace}:${var.service_account}"]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_issuer}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }

  # KEDA operator chain-assumes this role for SQS queue-depth polling of the
  # ingestion ScaledJob. KEDA's aws-eks identity provider authenticates as the
  # operator SA first, then assumes this workload role to call SQS
  # GetQueueAttributes. The operator role is owned by Phase 7
  # (webhook-ingress/infra/keda.tf); passed in here as an ARN. Mirrors the
  # gateway pattern (gateway-main.tf "gateway_agent" role). Issue #2213.
  dynamic "statement" {
    for_each = var.keda_operator_role_arn != "" ? [1] : []
    content {
      effect  = "Allow"
      actions = ["sts:AssumeRole"]

      principals {
        type        = "AWS"
        identifiers = [var.keda_operator_role_arn]
      }
    }
  }
}

resource "aws_iam_role" "agent_context" {
  name               = "${var.name_prefix}-irsa"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json

  tags = {
    Name        = "${var.name_prefix}-irsa"
    Environment = var.environment
  }
}

# --- Bedrock InvokeModel ---

resource "aws_iam_role_policy" "bedrock" {
  name = "${var.name_prefix}-bedrock"
  role = aws_iam_role.agent_context.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "BedrockInvoke"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ]
        Resource = [
          "arn:${local.partition}:bedrock:*::foundation-model/*",
          "arn:${local.partition}:bedrock:*:${var.account_id}:inference-profile/*",
        ]
      }
    ]
  })
}

# --- S3 Read/Write ---

resource "aws_iam_role_policy" "s3" {
  name = "${var.name_prefix}-s3"
  role = aws_iam_role.agent_context.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3BucketAccess"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:GetBucketLocation",
        ]
        Resource = [
          "arn:${local.partition}:s3:::${var.bucket_name}",
          "arn:${local.partition}:s3:::${var.bucket_name}/*",
        ]
      }
    ]
  })
}

# --- Secrets Manager Read ---

resource "aws_iam_role_policy" "secrets" {
  name = "${var.name_prefix}-secrets"
  role = aws_iam_role.agent_context.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SecretsManagerRead"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ]
        Resource = [
          "arn:${local.partition}:secretsmanager:${var.aws_region}:${var.account_id}:secret:adp/aws-e/*",
          "arn:${local.partition}:secretsmanager:${var.aws_region}:${var.account_id}:secret:agent-context/*",
        ]
      }
    ]
  })
}

# --- RDS IAM Auth (rds-db:connect for migration + stage_tracker) ---

resource "aws_iam_role_policy" "rds_connect" {
  name = "${var.name_prefix}-rds-connect"
  role = aws_iam_role.agent_context.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "RDSIAMConnect"
        Effect = "Allow"
        Action = ["rds-db:connect"]
        Resource = [
          "arn:${local.partition}:rds-db:${var.aws_region}:${var.account_id}:dbuser:*/${var.rds_username}",
        ]
      }
    ]
  })
}

# --- Resource Explorer + Tag API (for discover-infra.py) ---

resource "aws_iam_role_policy" "resource_explorer" {
  name = "${var.name_prefix}-resource-explorer"
  role = aws_iam_role.agent_context.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ResourceExplorer"
        Effect = "Allow"
        Action = [
          "resource-explorer-2:Search",
          "resource-explorer-2:GetView",
          "resource-explorer-2:ListViews",
          "tag:GetResources",
          "tag:GetTagKeys",
          "tag:GetTagValues",
        ]
        Resource = "*"
      }
    ]
  })
}

# --- OpenSearch Serverless (conditional on graphrag_enabled) ---

resource "aws_iam_role_policy" "opensearch" {
  count = var.graphrag_enabled ? 1 : 0
  name  = "${var.name_prefix}-opensearch"
  role  = aws_iam_role.agent_context.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "OpenSearchServerless"
        Effect   = "Allow"
        Action   = ["aoss:APIAccessAll"]
        Resource = "arn:${local.partition}:aoss:${var.aws_region}:${var.account_id}:collection/*"
      }
    ]
  })
}

# --- Neptune (conditional on neptune_enabled — independent of OpenSearch/GraphRAG) ---

resource "aws_iam_role_policy" "neptune" {
  count = var.neptune_enabled ? 1 : 0
  name  = "${var.name_prefix}-neptune"
  role  = aws_iam_role.agent_context.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "NeptuneAccess"
        Effect   = "Allow"
        Action   = ["neptune-db:*"]
        Resource = "arn:${local.partition}:neptune-db:${var.aws_region}:${var.account_id}:*/*"
      }
    ]
  })
}
