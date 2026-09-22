# =============================================================================
# Worker IRSA — shared IAM role for triage + static workers
# =============================================================================
# Issue #230: Copied from modules/agent-factory/infra/gateway-main.tf
# One role, one ServiceAccount. Both ScaledJobs reference the same SA.
# Permissions: SQS consume/send, DDB results, public YARA rules only.
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
        # Auto Mode uses the managed Pod Identity agent. Keep trust bound to
        # this cluster and worker service account, including transitive tags.
        Sid       = "CyberWorkerPodIdentity"
        Effect    = "Allow"
        Principal = { Service = "pods.eks.amazonaws.com" }
        Action    = ["sts:AssumeRole", "sts:TagSession"]
        Condition = {
          StringEquals = {
            "aws:RequestTag/eks-cluster-arn"            = aws_eks_cluster.cyber.arn
            "aws:RequestTag/kubernetes-namespace"       = "cyber-workers"
            "aws:RequestTag/kubernetes-service-account" = "cyber-worker"
          }
        }
      },
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

resource "aws_eks_pod_identity_association" "cyber_worker" {
  cluster_name    = aws_eks_cluster.cyber.name
  namespace       = "cyber-workers"
  service_account = "cyber-worker"
  role_arn        = aws_iam_role.cyber_worker.arn
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
# S3 — public rule data only; sample reads use broker capabilities
# ---------------------------------------------------------------------------

resource "aws_iam_role_policy" "cyber_worker_s3" {
  name = "s3-read-samples"
  role = aws_iam_role.cyber_worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
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
        Sid         = "DenyAmbientObjectReads", Effect = "Deny",
        Action      = ["s3:GetObject", "s3:GetObjectVersion"],
        NotResource = "arn:aws:s3:::adp-${var.environment}-cape-assets/yara-rules/public/*"
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# Secrets Manager — intentionally not granted (issue #5616)
# ---------------------------------------------------------------------------
# The worker role previously held secretsmanager:GetSecretValue on
# adp/cape/api-token-*. Neither worker reads a secret: there is no
# secretsmanager call and no boto3 Secrets Manager client anywhere in
# workers/ (triage and static both only use SQS, S3 and DynamoDB). CAPE
# submission is driven from the agent side, not from these pods.
#
# Removed rather than narrowed. These pods parse hostile binaries and, in
# Mode B, execute a generated script, so an unused credential grant here is
# exactly the privilege a successful sandbox escape would reach for — and
# because nothing uses it, removal cannot break a working path.
#
# If a worker ever needs a secret, add a grant for that specific secret at
# that time; do not restore this one on the assumption it was needed.

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
