# =============================================================================
# KEDA — Kubernetes Event-Driven Autoscaling on the cyber EKS cluster
# =============================================================================
# Issue #230: Copied from modules/agent-factory/infra/gateway-main.tf
# Deploys KEDA operator + IRSA role for SQS queue-depth polling.
# Uses provider aliases to target the cyber cluster (not the main cluster).
# =============================================================================

# ---------------------------------------------------------------------------
# KEDA Operator IAM Role (IRSA) — SQS queue polling only
# ---------------------------------------------------------------------------

resource "aws_iam_role" "cyber_keda_operator" {
  name = "${local.name_prefix}-keda-operator-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = local.cyber_oidc_provider_arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.cyber_oidc_issuer_short}:sub" = "system:serviceaccount:keda:keda-operator"
          "${local.cyber_oidc_issuer_short}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })

  tags = {
    Name      = "${local.name_prefix}-keda-operator-role"
    Component = "keda"
  }
}

resource "aws_iam_role_policy" "cyber_keda_sqs" {
  name = "sqs-scaler-read"
  role = aws_iam_role.cyber_keda_operator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SQSQueuePolling"
        Effect = "Allow"
        Action = [
          "sqs:GetQueueAttributes",
          "sqs:ListQueues"
        ]
        Resource = [
          aws_sqs_queue.cyber_triage_tasks.arn,
          aws_sqs_queue.cyber_triage_responses.arn,
          aws_sqs_queue.cyber_static_tasks.arn,
          aws_sqs_queue.cyber_static_responses.arn,
        ]
      },
      {
        Sid      = "AssumeWorkloadRole"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = aws_iam_role.cyber_worker.arn
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# KEDA Helm Release — targets the cyber cluster via provider alias
# ---------------------------------------------------------------------------

resource "helm_release" "cyber_keda" {
  provider = helm.cyber

  name             = "keda"
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  namespace        = "keda"
  version          = "2.16.0"
  create_namespace = true
  wait             = true
  timeout          = 600

  set {
    name  = "serviceAccount.create"
    value = "true"
  }

  set {
    name  = "serviceAccount.name"
    value = "keda-operator"
  }

  set {
    name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
    value = aws_iam_role.cyber_keda_operator.arn
  }

  set {
    name  = "metricsServer.enabled"
    value = "true"
  }

  set {
    name  = "resources.operator.requests.cpu"
    value = "100m"
  }

  set {
    name  = "resources.operator.requests.memory"
    value = "128Mi"
  }

  set {
    name  = "resources.operator.limits.cpu"
    value = "500m"
  }

  set {
    name  = "resources.operator.limits.memory"
    value = "512Mi"
  }

  # Prevent EKS Auto Mode node churn from evicting KEDA pods
  values = [
    yamlencode({
      podAnnotations = {
        keda           = { "karpenter.sh/do-not-disrupt" = "true" }
        metricsAdapter = { "karpenter.sh/do-not-disrupt" = "true" }
        webhooks       = { "karpenter.sh/do-not-disrupt" = "true" }
      }
    })
  ]

  depends_on = [
    aws_eks_cluster.cyber,
    aws_eks_access_policy_association.cyber_admins,
  ]
}
