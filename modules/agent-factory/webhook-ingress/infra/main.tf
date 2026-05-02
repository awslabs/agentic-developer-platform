# =============================================================================
# Webhook Ingress Infrastructure
# =============================================================================
# Foundational infrastructure for the hosted multi-tenant webhook ingress layer.
# Receives GitHub webhooks via REST API Gateway v1, validates with Lambda,
# and queues work onto SQS FIFO for downstream agent processing.
#
# State key: dev/modules/webhook-ingress/terraform.tfstate
# =============================================================================

terraform {
  backend "s3" {
    # Configured via -backend-config during terraform init
    # key = "<env>/modules/webhook-ingress/terraform.tfstate"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "adp-webhook-ingress"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# =============================================================================
# Kubernetes Provider — connects to EKS for namespace, SA, and KEDA manifests
# =============================================================================

data "aws_eks_cluster" "main" {
  name = var.eks_cluster_name
}

data "aws_eks_cluster_auth" "main" {
  name = var.eks_cluster_name
}

provider "kubernetes" {
  host                   = data.aws_eks_cluster.main.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.main.certificate_authority[0].data)
  token                  = data.aws_eks_cluster_auth.main.token
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# =============================================================================
# EKS OIDC + KEDA discovery
# =============================================================================
# The ScaledJob IRSA trust needs the cluster's OIDC provider ARN + issuer URL,
# and the KEDA operator role ARN for chain-assume. Discovered from AWS directly
# instead of passed in as variables — keeps the module self-contained and
# avoids terraform_remote_state reads to other modules (see tech-debt #337).

locals {
  name_prefix = "adp-${var.environment}"
  account_id  = data.aws_caller_identity.current.account_id

  # OIDC issuer URL from the EKS cluster — e.g.
  #   https://oidc.eks.us-east-1.amazonaws.com/id/ABC123...
  oidc_issuer = data.aws_eks_cluster.main.identity[0].oidc[0].issuer

  # OIDC provider ARN — derived from issuer URL + account ID. The provider
  # itself is registered once at platform setup; we only need its ARN here,
  # not ownership.
  oidc_provider_arn = "arn:aws:iam::${local.account_id}:oidc-provider/${replace(local.oidc_issuer, "https://", "")}"
}

data "aws_iam_role" "keda_operator" {
  name = var.keda_operator_role_name
}
