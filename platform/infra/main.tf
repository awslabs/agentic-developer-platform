# =============================================================================
# ADP Platform Infrastructure
# =============================================================================
# Shared infrastructure used by all ADP modules:
# - VPC and networking
# - EKS cluster
# - ECR container registry
# - Base IAM roles
# - CloudTrail logging
# =============================================================================

terraform {
  required_version = ">= 1.5"

  backend "s3" {
    # Configured via backend.tfvars
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
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "adp"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# -----------------------------------------------------------------------------
# Networking
# -----------------------------------------------------------------------------
module "networking" {
  source = "./modules/networking"

  environment         = var.environment
  vpc_cidr            = var.vpc_cidr
  availability_zones  = var.availability_zones
}

# -----------------------------------------------------------------------------
# EKS Cluster (Shared)
# -----------------------------------------------------------------------------
module "eks" {
  source = "./modules/eks"

  environment        = var.environment
  cluster_name       = "adp-${var.environment}-eks"
  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids

  # Node configuration
  node_instance_types = var.eks_node_instance_types
  node_desired_size   = var.eks_node_desired_size
  node_min_size       = var.eks_node_min_size
  node_max_size       = var.eks_node_max_size
}

# -----------------------------------------------------------------------------
# ECR Repositories
# -----------------------------------------------------------------------------
module "ecr" {
  source = "./modules/ecr"

  environment = var.environment
  repositories = [
    "adp-gateway",
    "adp-agent-runtime",
    "adp-skill-registry",
  ]
}

# -----------------------------------------------------------------------------
# Base IAM Roles
# -----------------------------------------------------------------------------
module "iam" {
  source = "./modules/iam"

  environment     = var.environment
  eks_cluster_arn = module.eks.cluster_arn
  eks_oidc_issuer = module.eks.oidc_issuer
}
