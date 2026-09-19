# Separate rollout acknowledgment: IAM session policies do not restrict EKS
# authentication. Inventory EVERY platform cluster/auth mode and remove source
# principal/account mappings before setting true. Do not re-add access while
# any issued task source session remains live. No EKS access deletion is automatic.
variable "agent_task_source_isolation_confirmed" {
  type        = bool
  default     = false
  description = "Approved isolation of the legacy worker principal from all platform Kubernetes authorization; requires a reviewed scoped rollout."
}

resource "aws_iam_role_policy" "gateway_task_source" {
  count = local.agent_authority_provisioned ? 1 : 0
  name  = "adp-${var.environment}-policy-gateway-task-source"
  role  = "adp-${var.environment}-role-gateway-service"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "CustomerTaskSource", Effect = "Allow", Action = ["sts:AssumeRole"], Resource = aws_iam_role.agent_scaledjob.arn },
      { Sid = "TaskSourceClusterAuthMode", Effect = "Allow", Action = ["eks:DescribeCluster"], Resource = data.aws_eks_cluster.main.arn },
      {
        Sid      = "TaskSourceAccessCheck", Effect = "Allow", Action = ["eks:DescribeAccessEntry"],
        Resource = "arn:aws:eks:${var.aws_region}:${local.account_id}:access-entry/${local.eks_cluster_name}/role/${local.account_id}/${aws_iam_role.agent_scaledjob.name}/*"
      }
    ]
  })
}

resource "kubernetes_role" "gateway_task_source_auth_read" {
  count = local.agent_authority_provisioned ? 1 : 0
  metadata {
    name      = "adp-${var.environment}-gateway-task-source-auth-read"
    namespace = "kube-system"
  }
  rule {
    api_groups     = [""]
    resources      = ["configmaps"]
    resource_names = ["aws-auth"]
    verbs          = ["get"]
  }
}

resource "kubernetes_role_binding" "gateway_task_source_auth_read" {
  count = local.agent_authority_provisioned ? 1 : 0
  metadata {
    name      = "adp-${var.environment}-gateway-task-source-auth-read"
    namespace = "kube-system"
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.gateway_task_source_auth_read[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = "gateway-service"
    namespace = var.gateway_namespace
  }
}

locals {
  agent_task_source_config = {
    "agent-task-source-role-arn"            = aws_iam_role.agent_scaledjob.arn
    "agent-task-source-eks-cluster"         = local.eks_cluster_name
    "agent-task-source-isolation-confirmed" = tostring(var.agent_task_source_isolation_confirmed)
  }
}

resource "aws_ssm_parameter" "agent_task_source" {
  for_each = local.agent_authority_provisioned ? local.agent_task_source_config : {}
  name     = "/adp/${var.environment}/gateway/${each.key}"
  type     = "SecureString"
  key_id   = aws_kms_key.dynamodb.arn
  value    = each.value
}

# Once legacy workers have drained, retain their role ARN solely as the source
# principal already trusted by customer accounts. Customer ExternalIds remain
# caller-controlled; the gateway's per-run STS session policy selects exact roles.
locals {
  agent_task_source_scoped_policy = {
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity", "sts:GetCallerIdentity"]
        Resource = "*"
      },
      {
        Effect    = "Deny"
        NotAction = ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity", "sts:GetCallerIdentity"]
        Resource  = "*"
      },
      {
        Effect   = "Deny"
        Action   = ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"]
        Resource = "arn:aws:iam::${local.account_id}:role/*"
      }
    ]
  }
}

resource "aws_iam_policy" "agent_task_source_boundary" {
  count  = var.agent_legacy_worker_admin_retired ? 1 : 0
  name   = "${local.name_prefix}-agent-task-source-boundary"
  policy = jsonencode(local.agent_task_source_scoped_policy)
}
