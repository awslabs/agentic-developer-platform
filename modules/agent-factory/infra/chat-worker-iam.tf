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

# Preserve existing non-model permissions for this focused identity split.
# Per-user data isolation is a separate change; these are still service-level grants.
resource "aws_iam_role_policy" "chat_worker_runtime" {
  for_each = {
    sqs            = aws_iam_role_policy.gateway_agent_sqs.policy
    sessions       = aws_iam_role_policy.gateway_agent_dynamodb.policy
    secrets        = aws_iam_role_policy.gateway_agent_secrets.policy
    bootstrap_logs = aws_iam_role_policy.gateway_agent_bootstrap_logs.policy
    gateway_invoke = aws_iam_role_policy.gateway_agent_execute_api.policy
    chat_data      = aws_iam_role_policy.gateway_agent_chat_dynamodb.policy
    chat_artifacts = aws_iam_role_policy.gateway_agent_chat_s3.policy
    chat_queue     = aws_iam_role_policy.gateway_agent_chat_sqs_fifo.policy
  }
  name   = "chat-${each.key}"
  role   = aws_iam_role.chat_worker.id
  policy = each.value
}

resource "aws_iam_role_policy" "chat_worker_deny_direct_bedrock" {
  name = "deny-direct-bedrock"
  role = aws_iam_role.chat_worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Deny"
      Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      Resource = "*"
    }]
  })
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
