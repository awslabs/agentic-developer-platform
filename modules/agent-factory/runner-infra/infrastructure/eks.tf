# EKS Cluster with Auto Mode
resource "aws_eks_cluster" "main" {
  name     = local.cluster_name
  role_arn = aws_iam_role.cluster.arn
  version  = var.cluster_version

  vpc_config {
    subnet_ids              = module.vpc.private_subnets
    endpoint_private_access = true
    endpoint_public_access  = true
  }

  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }

  # EKS Auto Mode configuration
  compute_config {
    enabled       = true
    node_pools    = ["general-purpose", "system"]
    node_role_arn = aws_iam_role.node.arn
  }

  kubernetes_network_config {
    elastic_load_balancing {
      enabled = true
    }
  }

  storage_config {
    block_storage {
      enabled = true
    }
  }

  # Don't bootstrap self-managed addons (Auto Mode handles them)
  bootstrap_self_managed_addons = false

  depends_on = [
    aws_iam_role_policy_attachment.cluster_AmazonEKSClusterPolicy,
    aws_iam_role_policy_attachment.cluster_AmazonEKSComputePolicy,
    aws_iam_role_policy_attachment.cluster_AmazonEKSBlockStoragePolicy,
    aws_iam_role_policy_attachment.cluster_AmazonEKSLoadBalancingPolicy,
    aws_iam_role_policy_attachment.cluster_AmazonEKSNetworkingPolicy,
    aws_iam_role_policy_attachment.cluster_AmazonEKSVPCResourceController,
  ]

  tags = {
    Name = local.cluster_name
  }
}

# OIDC Provider for IRSA
data "tls_certificate" "eks" {
  url = aws_eks_cluster.main.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "eks" {
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks.certificates[0].sha1_fingerprint]
  url             = aws_eks_cluster.main.identity[0].oidc[0].issuer

  tags = {
    Name = "${local.cluster_name}-oidc"
  }
}

# =============================================================================
# EKS Access Entry for GitHub Runner
# Allows runner pods to use kubectl with cluster-admin permissions
# =============================================================================

resource "aws_eks_access_entry" "runner" {
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = aws_iam_role.runner.arn
  type          = "STANDARD"

  tags = {
    Name = "${var.project_name}-runner-access"
  }
}

# =============================================================================
# Namespace-scoped EKS access (Issue #1204 — replaces cluster-admin)
# =============================================================================
# The runner-infra standalone cluster retains broad EKS API access via
# AmazonEKSEditPolicy scoped to the namespaces the runner actually operates in.
# Fine-grained K8s RBAC is enforced via kubernetes_role resources below.
# =============================================================================

resource "aws_eks_access_policy_association" "runner_edit" {
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = aws_iam_role.runner.arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy"

  access_scope {
    type       = "namespace"
    namespaces = ["adp-gateway", "adp-gateway-agents", "adp-agents", "arc-systems", "arc-runners", "agent-context", "keda"]
  }

  depends_on = [aws_eks_access_entry.runner]
}

# Namespace-manage ClusterRole — only operation that legitimately requires
# cluster-scope: creating/deleting specific named namespaces.
resource "kubernetes_cluster_role" "runner_namespace_manage" {
  metadata {
    name = "adp-runner-namespace-manage"

    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "runner-rbac"
    }
  }

  rule {
    api_groups     = [""]
    resources      = ["namespaces"]
    resource_names = ["arc-runners", "adp-gateway-agents", "adp-agents", "adp-gateway", "agent-context", "keda"]
    verbs          = ["get", "list", "create", "delete"]
  }
}

resource "kubernetes_cluster_role_binding" "runner_namespace_manage" {
  metadata {
    name = "adp-runner-namespace-manage"

    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "runner-rbac"
    }
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role.runner_namespace_manage.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "github-runner-sa"
    namespace = "arc-runners"
  }
}
