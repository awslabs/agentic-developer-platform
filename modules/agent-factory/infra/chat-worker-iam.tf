# Chat keeps its existing Kubernetes identity for gateway workload verification,
# but no longer shares the Python agent-gateway worker's direct Bedrock role.
resource "aws_iam_role" "chat_worker" {
  name                 = "adp-${var.environment}-chat-worker-role"
  permissions_boundary = var.automation_permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Federated = local.oidc_provider_arn }
        Action    = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${replace(local.oidc_issuer, "https://", "")}:sub" = "system:serviceaccount:${var.gateway_namespace}:adp-agent"
            "${replace(local.oidc_issuer, "https://", "")}:aud" = "sts.amazonaws.com"
          }
        }
      },
      {
        Effect    = "Allow"
        Principal = { AWS = data.aws_iam_role.keda_operator.arn }
        Action    = "sts:AssumeRole"
      }
    ]
  })
  tags = {
    Name      = "adp-${var.environment}-chat-worker-role"
    Component = "chat-agent"
  }
}

# KEDA only needs queue depth. The legacy container cannot consume, emit, or
# obtain model and owner-store authority while the delegated runtime is unready.
resource "aws_iam_role_policy" "chat_worker_queue_depth" {
  name = "chat-queue-depth-only"
  role = aws_iam_role.chat_worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:GetQueueAttributes"]
      Resource = aws_sqs_queue.chat_agent_tasks_fifo.arn
    }]
  })
}

resource "aws_iam_role_policy" "chat_worker_deny_direct_authority" {
  name = "deny-direct-chat-authority"
  role = aws_iam_role.chat_worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Deny"
      Action = [
        "bedrock:*", "dynamodb:*", "s3:*", "secretsmanager:*", "kms:*",
        "execute-api:Invoke", "sts:AssumeRole", "iam:PassRole",
        "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:SendMessage"
      ]
      Resource = "*"
    }]
  })
}

# A separate trusted queue supervisor may read the registered dispatch and ask
# the gateway to admit a sandbox. It cannot use the legacy worker's direct stores,
# model permissions, or another service account's web identity.
resource "aws_iam_role" "chat_supervisor" {
  name                 = "adp-${var.environment}-chat-supervisor-role"
  permissions_boundary = var.automation_permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${replace(local.oidc_issuer, "https://", "")}:sub" = "system:serviceaccount:${var.gateway_namespace}:adp-chat-supervisor"
          "${replace(local.oidc_issuer, "https://", "")}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
  tags = {
    Name      = "adp-${var.environment}-chat-supervisor-role"
    Component = "chat-agent"
  }
}

resource "aws_iam_role_policy" "chat_supervisor" {
  name = "scoped-chat-supervisor"
  role = aws_iam_role.chat_supervisor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadRegisteredChatDispatch"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"]
        Resource = aws_sqs_queue.chat_agent_tasks_fifo.arn
      },
      {
        Sid    = "AdmitVerifiedChatSandbox"
        Effect = "Allow"
        Action = ["execute-api:Invoke"]
        Resource = [
          "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/POST/agent/internal/v1/agent/chat/data/admit",
          "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/POST/agent/internal/v1/agent/chat/data/exit"
        ]
      },
      {
        Sid      = "NoModelOrDirectOwnerStore"
        Effect   = "Deny"
        Action   = ["bedrock:*", "dynamodb:*", "s3:*", "secretsmanager:*", "sqs:SendMessage", "sts:AssumeRole", "iam:PassRole"]
        Resource = "*"
      }
    ]
  })
}

# Kubernetes pod creation is NOT granted here. It must be installed only with
# enforcing supervisor-specific admission that prevents arbitrary pod creation.
resource "kubernetes_service_account" "chat_supervisor" {
  metadata {
    name      = "adp-chat-supervisor"
    namespace = kubernetes_namespace.gateway_agents.metadata[0].name
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.chat_supervisor.arn
    }
    labels = {
      "app.kubernetes.io/name"       = "adp-chat-supervisor"
      "app.kubernetes.io/part-of"    = "chat-agent"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
}

# The Python consumer still uses direct Bedrock, so isolate it from chat rather
# than removing its required permission. Its role cannot be assumed by adp-agent.
resource "kubernetes_service_account" "python_gateway_worker" {
  metadata {
    name      = "adp-gateway-worker"
    namespace = kubernetes_namespace.gateway_agents.metadata[0].name
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.gateway_agent.arn
    }
    labels = {
      "app.kubernetes.io/name"       = "adp-gateway-worker"
      "app.kubernetes.io/part-of"    = "agent-gateway"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
}

resource "kubernetes_service_account" "chat_sandbox" {
  metadata {
    name      = "adp-chat-sandbox"
    namespace = kubernetes_namespace.gateway_agents.metadata[0].name
    labels = {
      "app.kubernetes.io/name"       = "adp-chat-sandbox"
      "app.kubernetes.io/part-of"    = "chat-agent"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  automount_service_account_token = false
}
