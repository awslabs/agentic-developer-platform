data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_eks_cluster" "target" { name = var.cluster_name }
data "aws_iam_openid_connect_provider" "target" { arn = var.oidc_provider_arn }

locals {
  issuer = try(trimprefix(data.aws_eks_cluster.target.identity[0].oidc[0].issuer, "https://"), "")
  labels = {
    "app.kubernetes.io/name"    = "tool-adapters"
    "app.kubernetes.io/part-of" = "adp"
  }
}

# No policy attachment is created by the foundation. The adapter can start and
# expose private readiness without token retrieval or other provider access.
resource "aws_iam_role" "runtime" {
  name = var.runtime_role_name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = var.oidc_provider_arn }
      Condition = { StringEquals = {
        "${local.issuer}:aud" = "sts.amazonaws.com"
        "${local.issuer}:sub" = "system:serviceaccount:${var.namespace}:tool-adapters"
      } }
    }]
  })
  tags = { "adp:component" = "tool-adapters" }

  lifecycle {
    precondition {
      condition = (
        data.aws_caller_identity.current.account_id == var.aws_account_id &&
        data.aws_region.current.region == var.aws_region &&
        data.aws_eks_cluster.target.arn == "arn:aws:eks:${var.aws_region}:${var.aws_account_id}:cluster/${var.cluster_name}" &&
        data.aws_eks_cluster.target.status == "ACTIVE"
      )
      error_message = "The provider and EKS cluster must match the confirmed account, region and target."
    }
    precondition {
      condition = (
        var.oidc_provider_arn == "arn:aws:iam::${var.aws_account_id}:oidc-provider/${local.issuer}" &&
        trimprefix(data.aws_iam_openid_connect_provider.target.url, "https://") == local.issuer &&
        contains(data.aws_iam_openid_connect_provider.target.client_id_list, "sts.amazonaws.com")
      )
      error_message = "IRSA must use this cluster's existing OIDC provider and the STS audience."
    }
  }
}

resource "kubernetes_namespace_v1" "adapter" {
  metadata {
    name = var.namespace
    labels = merge(local.labels, {
      "pod-security.kubernetes.io/enforce"         = "restricted"
      "pod-security.kubernetes.io/enforce-version" = "latest"
    })
  }
  # Validate the AWS target and IRSA binding before provisioning its namespace.
  # The parent separately binds the Kubernetes provider to that same target.
  depends_on = [aws_iam_role.runtime]
}

resource "kubernetes_role_v1" "verifier" {
  metadata {
    name      = "adp-tool-adapters-verifier"
    namespace = kubernetes_namespace_v1.adapter.metadata[0].name
    labels    = local.labels
  }
  rule {
    api_groups     = [""]
    resources      = ["serviceaccounts", "services"]
    resource_names = ["tool-adapters"]
    verbs          = ["get"]
  }
  rule {
    api_groups     = [""]
    resources      = ["configmaps"]
    resource_names = ["tool-adapters-contract"]
    verbs          = ["get"]
  }
  rule {
    api_groups     = ["apps"]
    resources      = ["deployments"]
    resource_names = ["tool-adapters"]
    verbs          = ["get"]
  }
  rule {
    api_groups     = ["policy"]
    resources      = ["poddisruptionbudgets"]
    resource_names = ["tool-adapters"]
    verbs          = ["get"]
  }
  # Controller-generated names are discovered by a bounded label-selected list,
  # then independently reread by the observer. Kubernetes RBAC cannot restrict a
  # list by labels; this is why access is confined to the dedicated namespace.
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = ["apps"]
    resources  = ["replicasets"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = ["discovery.k8s.io"]
    resources  = ["endpointslices"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups     = [""]
    resources      = ["services/proxy"]
    resource_names = ["http:tool-adapters:80"]
    verbs          = ["get"]
  }
}

resource "kubernetes_role_binding_v1" "verifier" {
  metadata {
    name      = "adp-tool-adapters-verifier"
    namespace = kubernetes_namespace_v1.adapter.metadata[0].name
    labels    = local.labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.verifier.metadata[0].name
  }
  subject {
    kind      = "Group"
    name      = var.runtime_verifier_group
    api_group = "rbac.authorization.k8s.io"
  }
}
