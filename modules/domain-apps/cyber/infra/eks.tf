# =============================================================================
# Cyber EKS Cluster — Threat Research triage + static analysis workers
# =============================================================================
# Issue #230: Separate EKS cluster in the Threat Research VPC.
# Copied from platform/infra/modules/eks/ — standalone IAM roles, own OIDC.
# Hard invariant #1: zero IAM trust to the main cluster's OIDC.
# =============================================================================

# ---------------------------------------------------------------------------
# IAM Roles — separate from the main platform cluster
# ---------------------------------------------------------------------------

resource "aws_iam_role" "cyber_eks_cluster" {
  name = "${local.name_prefix}-eks-cluster-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "eks.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })

  tags = {
    Name = "${local.name_prefix}-eks-cluster-role"
  }
}

resource "aws_iam_role_policy_attachment" "cyber_eks_cluster_policy" {
  role       = aws_iam_role.cyber_eks_cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_iam_role_policy_attachment" "cyber_eks_compute_policy" {
  role       = aws_iam_role.cyber_eks_cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSComputePolicy"
}

resource "aws_iam_role_policy_attachment" "cyber_eks_block_storage_policy" {
  role       = aws_iam_role.cyber_eks_cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSBlockStoragePolicy"
}

resource "aws_iam_role_policy_attachment" "cyber_eks_lb_policy" {
  role       = aws_iam_role.cyber_eks_cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSLoadBalancingPolicy"
}

resource "aws_iam_role_policy_attachment" "cyber_eks_networking_policy" {
  role       = aws_iam_role.cyber_eks_cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSNetworkingPolicy"
}

resource "aws_iam_role" "cyber_eks_node" {
  name = "${local.name_prefix}-eks-node-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })

  tags = {
    Name = "${local.name_prefix}-eks-node-role"
  }

  # EKS auto-adds the `eks:eks-cluster-name` tag to node roles attached to
  # a cluster. Untagging requires `iam:UntagRole`, which the CI runner
  # role's permissions boundary does not grant. Let the service manage
  # this tag — we never touch it ourselves.
  lifecycle {
    ignore_changes = [tags["eks:eks-cluster-name"], tags_all["eks:eks-cluster-name"]]
  }
}

resource "aws_iam_role_policy_attachment" "cyber_eks_node_worker" {
  role       = aws_iam_role.cyber_eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "cyber_eks_node_cni" {
  role       = aws_iam_role.cyber_eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role_policy_attachment" "cyber_eks_node_ecr" {
  role       = aws_iam_role.cyber_eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# ---------------------------------------------------------------------------
# Security Group for EKS cluster
# ---------------------------------------------------------------------------

resource "aws_security_group" "cyber_eks" {
  name        = "${local.name_prefix}-sg-eks"
  description = "Security group for the cyber EKS cluster"
  vpc_id      = aws_vpc.threat_research.id

  ingress {
    description = "Allow pods to communicate with the cluster API Server"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    self        = true
  }

  egress {
    description = "Allow all outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-sg-eks"
  }
}

# ---------------------------------------------------------------------------
# EKS Cluster — Auto Mode enabled
# ---------------------------------------------------------------------------
# Note: KMS encryption for secrets is omitted because the runner role lacks
# kms:TagResource. AWS default encryption (AES-256) is sufficient for dev.
# Add a KMS key in prod via the same pattern as platform/infra/modules/eks/.

resource "aws_eks_cluster" "cyber" {
  name     = "${local.name_prefix}-eks"
  role_arn = aws_iam_role.cyber_eks_cluster.arn
  version  = var.cyber_eks_cluster_version

  bootstrap_self_managed_addons = false

  access_config {
    authentication_mode = "API_AND_CONFIG_MAP"
  }

  vpc_config {
    subnet_ids              = aws_subnet.private[*].id
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = var.cyber_eks_public_access_cidrs
    security_group_ids      = [aws_security_group.cyber_eks.id]
  }

  # EKS Auto Mode
  compute_config {
    enabled       = true
    node_pools    = ["general-purpose"]
    node_role_arn = aws_iam_role.cyber_eks_node.arn
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

  enabled_cluster_log_types = ["api", "audit", "authenticator"]

  depends_on = [
    aws_iam_role_policy_attachment.cyber_eks_cluster_policy,
    aws_iam_role_policy_attachment.cyber_eks_compute_policy,
    aws_iam_role_policy_attachment.cyber_eks_block_storage_policy,
    aws_iam_role_policy_attachment.cyber_eks_lb_policy,
    aws_iam_role_policy_attachment.cyber_eks_networking_policy,
  ]

  tags = {
    Name = "${local.name_prefix}-eks"
  }
}

# ---------------------------------------------------------------------------
# OIDC Provider for IRSA
# ---------------------------------------------------------------------------

data "tls_certificate" "cyber_cluster" {
  url = aws_eks_cluster.cyber.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "cyber_cluster" {
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.cyber_cluster.certificates[0].sha1_fingerprint]
  url             = aws_eks_cluster.cyber.identity[0].oidc[0].issuer

  tags = {
    Name = "${local.name_prefix}-eks-oidc"
  }
}

# ---------------------------------------------------------------------------
# EKS Access Entries — cluster admins
# ---------------------------------------------------------------------------

resource "aws_eks_access_entry" "cyber_admins" {
  for_each      = toset(var.cyber_cluster_admin_principal_arns)
  cluster_name  = aws_eks_cluster.cyber.name
  principal_arn = each.key
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "cyber_admins" {
  # Iterate over the variable (static keys) rather than the access_entry
  # resource output — the latter forces `-target` on first apply because
  # Terraform can't know the keys until the entries exist.
  for_each      = toset(var.cyber_cluster_admin_principal_arns)
  cluster_name  = aws_eks_cluster.cyber.name
  principal_arn = each.key
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.cyber_admins]
}

# ---------------------------------------------------------------------------
# Locals — OIDC issuer for IRSA trust policies
# ---------------------------------------------------------------------------

locals {
  cyber_oidc_issuer       = aws_eks_cluster.cyber.identity[0].oidc[0].issuer
  cyber_oidc_issuer_short = replace(local.cyber_oidc_issuer, "https://", "")
  cyber_oidc_provider_arn = aws_iam_openid_connect_provider.cyber_cluster.arn
}

# ---------------------------------------------------------------------------
# Kubernetes + Helm providers — aliased for the cyber cluster
# ---------------------------------------------------------------------------

provider "kubernetes" {
  alias                  = "cyber"
  host                   = aws_eks_cluster.cyber.endpoint
  cluster_ca_certificate = base64decode(aws_eks_cluster.cyber.certificate_authority[0].data)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", aws_eks_cluster.cyber.name]
  }
}

provider "helm" {
  alias = "cyber"
  kubernetes {
    host                   = aws_eks_cluster.cyber.endpoint
    cluster_ca_certificate = base64decode(aws_eks_cluster.cyber.certificate_authority[0].data)

    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", aws_eks_cluster.cyber.name]
    }
  }
}
