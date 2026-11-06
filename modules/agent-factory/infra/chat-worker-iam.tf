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
  count = length(var.chat_legacy_upgrade_policies) == 0 || var.chat_legacy_upgrade_deny_ready ? 1 : 0
  name  = "deny-direct-chat-authority"
  role  = aws_iam_role.chat_worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Deny"
      Action = [
        "bedrock:*", "bedrock-agentcore:*", "dynamodb:*", "s3:*", "secretsmanager:*", "kms:*",
        "execute-api:Invoke", "sts:AssumeRole", "iam:PassRole",
        "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:SendMessage"
      ]
      Resource = "*"
    }]
  })
}

moved {
  from = aws_iam_role_policy.chat_worker_deny_direct_authority
  to   = aws_iam_role_policy.chat_worker_deny_direct_authority[0]
}

# The full upgrade first publishes the unavailable-chat ingress response while
# preserving the observed legacy grants. After the script has drained jobs it
# removes this temporary input and applies the normal retired identity above.
# Never populate this from defaults or use it to activate the legacy runtime.
variable "chat_legacy_upgrade_policies" {
  type        = map(string)
  default     = {}
  description = "Transient, observed inline policies for the ordered legacy chat retirement."
  validation {
    condition = alltrue([for name in keys(var.chat_legacy_upgrade_policies) : contains([
      "sqs", "sessions", "secrets", "bootstrap_logs", "gateway_invoke",
      "chat_data", "chat_artifacts", "chat_queue", "deny-direct-bedrock"
    ], name)])
    error_message = "Only the known legacy chat inline policies may be retained."
  }
}

variable "chat_legacy_upgrade_deny_ready" {
  type        = bool
  default     = false
  description = "Transient retirement stage: legacy jobs have drained and the broader denial may be attached before old policies are removed."
}

resource "aws_iam_role_policy" "chat_worker_runtime" {
  for_each = { for name, document in var.chat_legacy_upgrade_policies : name => document if name != "deny-direct-bedrock" }
  name     = "chat-${each.key}"
  role     = aws_iam_role.chat_worker.id
  policy   = each.value
}

moved {
  from = aws_iam_role_policy.chat_worker_deny_direct_bedrock
  to   = aws_iam_role_policy.chat_worker_deny_direct_bedrock[0]
}

resource "aws_iam_role_policy" "chat_worker_deny_direct_bedrock" {
  count  = contains(keys(var.chat_legacy_upgrade_policies), "deny-direct-bedrock") ? 1 : 0
  name   = "deny-direct-bedrock"
  role   = aws_iam_role.chat_worker.id
  policy = var.chat_legacy_upgrade_policies["deny-direct-bedrock"]
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
        # Every route the supervisor calls (sandbox-supervisor-admission.ts), as
        # reached through the gateway API's AWS_IAM `/internal/{proxy+}` method:
        # <stage>/internal/v1/agent/chat/... is forwarded to the internal-plane ALB
        # with the path preserved. The `/agent/{proxy+}` method strips its prefix
        # and targets the edge ALB, so `/agent/internal/...` is not the form the
        # supervisor signs. The gateway still verifies the body-bound producer
        # proof on each route; this grant only lets the request through API Gateway.
        Sid    = "AdmitVerifiedChatSandbox"
        Effect = "Allow"
        Action = ["execute-api:Invoke"]
        Resource = [
          "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/POST/internal/v1/agent/chat/data/reserve",
          "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/POST/internal/v1/agent/chat/data/admit",
          "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/POST/internal/v1/agent/chat/data/exit",
          "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/POST/internal/v1/agent/chat/data/teardown",
          "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/POST/internal/v1/agent/chat/data/finalize",
          "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/POST/internal/v1/agent/chat/data/complete",
          "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/POST/internal/v1/agent/chat/data/resume",
          "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/POST/internal/v1/agent/chat/data/session/commit",
          "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/POST/internal/v1/agent/chat/crew-run",
          "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/POST/internal/v1/agent/chat/crew-run/lease",
          "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/POST/internal/v1/agent/chat/crew-run/activate",
          "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/POST/internal/v1/agent/chat/crew-run/ready"
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
