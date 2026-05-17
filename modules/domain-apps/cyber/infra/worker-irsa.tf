# =============================================================================
# Worker IRSA — shared IAM role for triage + static workers
# =============================================================================
# Issue #230: Copied from modules/agent-factory/infra/gateway-main.tf
# One role, one ServiceAccount. Both ScaledJobs reference the same SA.
# Permissions: SQS consume/send, DDB write, S3 read samples, Secrets read.
# Hard invariant #3: No Bedrock, no other tenant's S3 prefix, no broad Secrets.
# =============================================================================

# ---------------------------------------------------------------------------
# IAM Role — cyber worker
# ---------------------------------------------------------------------------

resource "aws_iam_role" "cyber_worker" {
  name = "${local.name_prefix}-worker-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # IRSA: worker pods assume this role via their ServiceAccount
        Effect = "Allow"
        Principal = {
          Federated = local.cyber_oidc_provider_arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${local.cyber_oidc_issuer_short}:sub" = "system:serviceaccount:cyber-workers:cyber-worker"
            "${local.cyber_oidc_issuer_short}:aud" = "sts.amazonaws.com"
          }
        }
      },
      {
        # KEDA operator chain-assumes this role for SQS queue-depth polling
        Effect = "Allow"
        Principal = {
          AWS = aws_iam_role.cyber_keda_operator.arn
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name      = "${local.name_prefix}-worker-role"
    Component = "cyber-worker"
  }
}

# ---------------------------------------------------------------------------
# SQS — consume tasks, send responses
# ---------------------------------------------------------------------------

resource "aws_iam_role_policy" "cyber_worker_sqs" {
  name = "sqs-access"
  role = aws_iam_role.cyber_worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ConsumeTaskQueues"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ]
        Resource = [
          aws_sqs_queue.cyber_triage_tasks.arn,
          aws_sqs_queue.cyber_static_tasks.arn,
        ]
      },
      {
        Sid    = "SendToResponseQueues"
        Effect = "Allow"
        Action = ["sqs:SendMessage"]
        Resource = [
          aws_sqs_queue.cyber_triage_responses.arn,
          aws_sqs_queue.cyber_static_responses.arn,
        ]
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# DynamoDB — write analysis results
# ---------------------------------------------------------------------------

resource "aws_iam_role_policy" "cyber_worker_dynamodb" {
  name = "dynamodb-results"
  role = aws_iam_role.cyber_worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query"
        ]
        Resource = [
          aws_dynamodb_table.cyber_analysis_results.arn,
          "${aws_dynamodb_table.cyber_analysis_results.arn}/index/*",
        ]
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
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# S3 — read sample artifacts from the chat artifacts bucket
# ---------------------------------------------------------------------------

resource "aws_iam_role_policy" "cyber_worker_s3" {
  name = "s3-read-samples"
  role = aws_iam_role.cyber_worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadSampleArtifacts"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "arn:aws:s3:::adp-${var.environment}-chat-artifacts-*/o/*/in/*"
      },
      {
        # Issue #272: Workers fetch YARA rules from S3 via initContainer
        Sid    = "ReadYaraRulesPublic"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          "arn:aws:s3:::adp-${var.environment}-cape-assets",
          "arn:aws:s3:::adp-${var.environment}-cape-assets/yara-rules/public/*"
        ]
      },
      {
        # Issue #278: Workers need to read samples from cape-assets bucket
        # (smoke-test samples, future: any sample staged for analysis)
        Sid      = "ReadCapeAssetsSamples"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "arn:aws:s3:::adp-${var.environment}-cape-assets/smoke-test/*"
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# Secrets Manager — CAPE API token only
# ---------------------------------------------------------------------------

resource "aws_iam_role_policy" "cyber_worker_secrets" {
  name = "secrets-cape-token"
  role = aws_iam_role.cyber_worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = "arn:aws:secretsmanager:${var.aws_region}:${var.account_id}:secret:adp/cape/api-token-*"
    }]
  })
}

# ---------------------------------------------------------------------------
# Kubernetes Namespace + ServiceAccount
# ---------------------------------------------------------------------------

resource "kubernetes_namespace" "cyber_workers" {
  provider = kubernetes.cyber

  metadata {
    name = "cyber-workers"
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-cyber"
      "app.kubernetes.io/component"  = "cyber-workers"
    }
  }

  depends_on = [
    aws_eks_cluster.cyber,
    aws_eks_access_policy_association.cyber_admins,
  ]
}

resource "kubernetes_service_account" "cyber_worker" {
  provider = kubernetes.cyber

  metadata {
    name      = "cyber-worker"
    namespace = kubernetes_namespace.cyber_workers.metadata[0].name

    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.cyber_worker.arn
    }

    labels = {
      "app.kubernetes.io/name"       = "cyber-worker"
      "app.kubernetes.io/part-of"    = "adp-cyber"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
}
