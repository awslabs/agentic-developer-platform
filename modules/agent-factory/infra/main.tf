# =============================================================================
# Agent Factory Infrastructure
# =============================================================================
# Agent-specific resources that layer on top of the shared platform infra.
# Uses remote state to reference the shared VPC, EKS cluster, and ECR.
# =============================================================================

terraform {
  required_version = ">= 1.5"

  backend "s3" {
    # Configured via -backend-config during terraform init
    # See environments/dev/modules/agent-factory-backend.tfvars
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.23"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.12"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "adp-agent-factory"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# =============================================================================
# Shared Platform Remote State
# =============================================================================
# Read outputs from the shared platform infrastructure (VPC, EKS, ECR, IAM)
# deployed via platform/infra/. This avoids duplicating networking and compute.
# =============================================================================

data "terraform_remote_state" "platform" {
  backend = "s3"
  config = {
    bucket = "adp-terraform-state-${var.account_id}"
    key    = "${var.environment}/platform/terraform.tfstate"
    region = var.aws_region
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  # Shared platform resources
  cluster_name    = data.terraform_remote_state.platform.outputs.eks_cluster_name
  cluster_endpoint = data.terraform_remote_state.platform.outputs.eks_cluster_endpoint
  cluster_ca      = data.terraform_remote_state.platform.outputs.eks_cluster_ca_certificate
  oidc_issuer     = data.terraform_remote_state.platform.outputs.eks_oidc_issuer
  oidc_provider_arn = data.terraform_remote_state.platform.outputs.eks_oidc_provider_arn
  vpc_id          = data.terraform_remote_state.platform.outputs.vpc_id
  private_subnets = data.terraform_remote_state.platform.outputs.private_subnet_ids

  name_prefix = "adp-${var.environment}-agent"
}

# =============================================================================
# Kubernetes & Helm Providers (using shared EKS cluster)
# =============================================================================

provider "kubernetes" {
  host                   = local.cluster_endpoint
  cluster_ca_certificate = base64decode(local.cluster_ca)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", local.cluster_name]
  }
}

provider "helm" {
  kubernetes {
    host                   = local.cluster_endpoint
    cluster_ca_certificate = base64decode(local.cluster_ca)

    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", local.cluster_name]
    }
  }
}

# =============================================================================
# Runner IAM (IRSA role for GitHub Actions runner pods)
# =============================================================================

module "runner_iam" {
  source = "./modules/runner-iam"

  environment       = var.environment
  name_prefix       = local.name_prefix
  oidc_provider_arn = local.oidc_provider_arn
  oidc_issuer       = local.oidc_issuer
  account_id        = data.aws_caller_identity.current.account_id
  aws_region        = var.aws_region
  runner_namespace  = var.runner_namespace
}

# =============================================================================
# Secrets Manager (GitHub App credentials)
# =============================================================================

module "secrets" {
  source = "./modules/secrets"

  environment = var.environment
  name_prefix = local.name_prefix
}

# =============================================================================
# Beads State (DynamoDB + S3 for issue tracking)
# =============================================================================

module "beads_state" {
  source = "./modules/beads-state"

  environment = var.environment
  name_prefix = local.name_prefix
  account_id  = data.aws_caller_identity.current.account_id
}

# =============================================================================
# ARC Runner (Helm release for Actions Runner Controller)
# =============================================================================

module "arc_runner" {
  source = "./modules/arc-runner"

  environment      = var.environment
  name_prefix      = local.name_prefix
  cluster_name     = local.cluster_name
  runner_namespace = var.runner_namespace
  github_org       = var.github_org
  runner_role_arn  = module.runner_iam.runner_role_arn

  depends_on = [module.runner_iam]
}

# =============================================================================
# EKS Access Entry for Runner Pods
# =============================================================================

resource "aws_eks_access_entry" "runner" {
  cluster_name  = local.cluster_name
  principal_arn = module.runner_iam.runner_role_arn
  type          = "STANDARD"

  tags = {
    Name = "${local.name_prefix}-runner-access"
  }
}

resource "aws_eks_access_policy_association" "runner_admin" {
  cluster_name  = local.cluster_name
  principal_arn = module.runner_iam.runner_role_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.runner]
}
