# =============================================================================
# KEDA Operator — Helm release + IAM Role (IRSA)
# =============================================================================
# Phase 7 is the canonical owner of KEDA. The operator + CRDs must be present
# before any ScaledJob can be applied (both Phase 7's webhook worker and
# Phase 8's chat-agent gateway worker depend on KEDA).
#
# The IAM role grants KEDA least-privilege SQS read access for queue-depth
# polling. Phase 8 adds its own aws_iam_role_policy on this role for its
# gateway queues (referenced by role name).
#
# Issue: #1052
# =============================================================================

# =============================================================================
# KEDA Operator IAM Role (IRSA) — for SQS queue polling
# =============================================================================
# The KEDA operator needs to call sqs:GetQueueAttributes to check queue depth
# and decide whether to scale worker pods. Without this role, KEDA falls back
# to EC2 IMDS which doesn't exist on EKS Auto Mode, causing KEDAScalerFailed.
#
# This is a separate, least-privilege role — it only gets SQS read access,
# not the full Bedrock/DDB/Secrets permissions the worker pods need.
# =============================================================================

resource "aws_iam_role" "keda_operator" {
  name = "adp-${var.environment}-keda-operator-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = local.oidc_provider_arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${replace(local.oidc_issuer, "https://", "")}:sub" = "system:serviceaccount:keda:keda-operator"
          "${replace(local.oidc_issuer, "https://", "")}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })

  tags = {
    Name      = "adp-${var.environment}-keda-operator-role"
    Component = "keda"
  }
}

resource "aws_iam_role_policy" "keda_operator_sqs" {
  name = "sqs-scaler-read"
  role = aws_iam_role.keda_operator.id

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
          aws_sqs_queue.agent_submit.arn,
          aws_sqs_queue.agent_submit_dlq.arn,
        ]
      },
      {
        # KEDA's aws-eks identity provider chain-assumes the workload pod's
        # IRSA role when checking queue depth. Allow this AssumeRole so the
        # scaler can authenticate.
        Sid      = "AssumeWorkloadRole"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = aws_iam_role.agent_scaledjob.arn
      }
    ]
  })
}

# =============================================================================
# KEDA Helm Release
# =============================================================================

resource "helm_release" "keda" {
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

  # IRSA annotation so KEDA can authenticate to SQS for queue-depth polling.
  # Managed via Helm values so the annotation survives Helm reconciliation.
  set {
    name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
    value = aws_iam_role.keda_operator.arn
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

  # Prevent EKS Auto Mode's Karpenter from evicting the KEDA operator /
  # metrics-apiserver / admission-webhook pods during node consolidation.
  # Without this, the scaler can disappear at arbitrary times, breaking the
  # 1-second SQS polling and leaving messages stuck until a new operator
  # schedules.
  #
  # `values = [yamlencode(...)]` is used instead of `set {}` because the helm
  # provider v2.x's `set` coerces the string "true" into a YAML bool, which
  # fails Kubernetes annotation validation (annotations must be strings). The
  # yamlencode path preserves the quoted-string typing.
  values = [
    yamlencode({
      podAnnotations = {
        keda           = { "karpenter.sh/do-not-disrupt" = "true" }
        metricsAdapter = { "karpenter.sh/do-not-disrupt" = "true" }
        webhooks       = { "karpenter.sh/do-not-disrupt" = "true" }
      }
    })
  ]
}
