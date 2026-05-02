# =============================================================================
# Webhook Ingress Infrastructure
# =============================================================================
# Foundational infrastructure for the hosted multi-tenant webhook ingress layer.
# Receives GitHub webhooks via HTTP API Gateway v2, validates with a stub Lambda,
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

locals {
  name_prefix = "adp-${var.environment}"
  account_id  = data.aws_caller_identity.current.account_id
}
