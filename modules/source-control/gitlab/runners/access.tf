variable "deployment_role_arn" {
  description = "Optional existing protected GitLab deployment role; bind namespace-only EKS administration after creating the inert namespaces."
  type        = string
  default     = null
  validation {
    condition = var.deployment_role_arn == null ? true : can(regex(
      "^arn:aws:iam::[0-9]{12}:role/adp-${var.environment}-gitlab-trusted-deployment$", var.deployment_role_arn
    ))
    error_message = "Bind the existing dedicated GitLab deployment role in this environment."
  }
}

resource "aws_eks_access_entry" "deployment" {
  count         = var.deployment_role_arn == null ? 0 : 1
  cluster_name  = data.terraform_remote_state.platform.outputs.eks_cluster_name
  principal_arn = var.deployment_role_arn
  type          = "STANDARD"
  tags          = { Component = "gitlab-runners", Environment = var.environment }
  lifecycle {
    precondition {
      condition     = split(":", var.deployment_role_arn)[4] == data.aws_caller_identity.current.account_id
      error_message = "The protected deployment role must belong to the runner account."
    }
  }
}

resource "aws_eks_access_policy_association" "deployment" {
  count         = var.deployment_role_arn == null ? 0 : 1
  cluster_name  = aws_eks_access_entry.deployment[0].cluster_name
  principal_arn = aws_eks_access_entry.deployment[0].principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSAdminPolicy"
  access_scope {
    type       = "namespace"
    namespaces = sort([for group in local.group_bindings : group.manager_namespace])
  }
  depends_on = [kubernetes_namespace_v1.manager]
}
