# =============================================================================
# ADP Platform Infrastructure
# =============================================================================
# Shared infrastructure used by all ADP modules:
# - VPC and networking
# - EKS cluster
# - ECR container registry
# - Base IAM roles
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
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  name_prefix  = coalesce(var.name_prefix, "adp-${var.environment}")
  state_bucket = coalesce(var.state_bucket, "adp-terraform-state-${data.aws_caller_identity.current.account_id}")
  common_tags = {
    Project     = "adp"
    Environment = var.environment
    ManagedBy   = "terraform"
  }

  # Deployer principal that should get EKS cluster-admin.
  # If the caller is an assumed role (e.g. arn:aws:sts::<acct>:assumed-role/Admin/session),
  # reduce it to the underlying IAM role ARN so the access entry is stable.
  caller_arn = data.aws_caller_identity.current.arn
  deployer_role_arn = (
    length(regexall("^arn:aws:sts::[0-9]+:assumed-role/", local.caller_arn)) > 0
    ? replace(
      replace(local.caller_arn, "/^arn:aws:sts::/", "arn:aws:iam::"),
      "/:assumed-role/([^/]+)/.*$/",
      ":role/$1"
    )
    : local.caller_arn
  )

  cluster_admin_principal_arns = distinct(concat(
    [local.deployer_role_arn],
    var.extra_cluster_admin_principal_arns,
  ))
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}

# Configure the kubernetes provider against the cluster created below.
# On the first apply the cluster doesn't exist yet — the provider only evaluates
# on resources that reference it, and those resources have depends_on set.
provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_ca_certificate)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", var.aws_region]
  }
}

# -----------------------------------------------------------------------------
# Networking
# -----------------------------------------------------------------------------
module "networking" {
  source = "./modules/networking"

  environment = var.environment
  aws_region  = var.aws_region
  name_prefix = local.name_prefix
  common_tags = local.common_tags

  vpc_cidr           = var.vpc_cidr
  az_count           = var.az_count
  single_nat_gateway = var.single_nat_gateway
}

# -----------------------------------------------------------------------------
# Base IAM Roles (cluster + node group service roles)
# -----------------------------------------------------------------------------
module "iam" {
  source = "./modules/iam"

  environment             = var.environment
  name_prefix             = local.name_prefix
  common_tags             = local.common_tags
  cluster_name            = "${local.name_prefix}-eks-cluster"
  create_instance_profile = var.create_instance_profile

  # Leave downstream feature flags at defaults (disabled) — this platform layer
  # only provisions the *baseline* roles. Feature-specific policies (RDS IAM,
  # chat logging, etc.) are added by the modules that turn them on.
  enable_rds_iam_auth         = false
  enable_elasticache_iam_auth = false
  enable_chat_logging         = false
  enable_comprehend_pii       = false
  enable_xray_tracing         = false
}

# -----------------------------------------------------------------------------
# EKS Cluster (Auto Mode)
# -----------------------------------------------------------------------------
module "eks" {
  source = "./modules/eks"

  environment = var.environment
  name_prefix = local.name_prefix
  common_tags = local.common_tags

  vpc_id                = module.networking.vpc_id
  private_subnet_ids    = module.networking.private_subnet_ids
  eks_security_group_id = module.networking.eks_security_group_id

  eks_cluster_role_arn         = module.iam.eks_cluster_role_arn
  node_group_role_arn          = module.iam.eks_node_group_role_arn
  eks_public_access_cidrs      = var.eks_public_access_cidrs
  cluster_admin_principal_arns = local.cluster_admin_principal_arns

  cluster_version           = var.eks_cluster_version
  node_group_instance_types = var.eks_node_instance_types
  node_group_desired_size   = var.eks_node_desired_size
  node_group_min_size       = var.eks_node_min_size
  node_group_max_size       = var.eks_node_max_size

  enable_container_insights = var.enable_container_insights
}

# -----------------------------------------------------------------------------
# ECR Repositories
# -----------------------------------------------------------------------------
module "ecr" {
  source = "./modules/ecr"

  environment  = var.environment
  name_prefix  = local.name_prefix
  common_tags  = local.common_tags
  repositories = var.ecr_repositories
}

# -----------------------------------------------------------------------------
# CodeBuild Projects (docker-requiring builds only)
# -----------------------------------------------------------------------------
module "codebuild" {
  source = "./modules/codebuild"

  name_prefix  = local.name_prefix
  state_bucket = local.state_bucket
  common_tags  = local.common_tags
}

# -----------------------------------------------------------------------------
# Security Scans (S3 archival for SARIF results)
# -----------------------------------------------------------------------------
module "security_scans" {
  source = "./modules/security-scans"

  environment = var.environment
}
