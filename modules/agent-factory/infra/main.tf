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
    bucket = "adp-terraform-state-${data.aws_caller_identity.current.account_id}"
    key    = "${var.environment}/platform/terraform.tfstate"
    region = var.aws_region
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  # Shared platform resources
  cluster_name      = data.terraform_remote_state.platform.outputs.eks_cluster_name
  cluster_endpoint  = data.terraform_remote_state.platform.outputs.eks_cluster_endpoint
  cluster_ca        = data.terraform_remote_state.platform.outputs.eks_cluster_ca_certificate
  oidc_issuer       = data.terraform_remote_state.platform.outputs.eks_oidc_issuer
  oidc_provider_arn = data.terraform_remote_state.platform.outputs.eks_oidc_provider_arn
  vpc_id            = data.terraform_remote_state.platform.outputs.vpc_id
  private_subnets   = data.terraform_remote_state.platform.outputs.private_subnet_ids

  name_prefix = "adp-${var.environment}-agent"

  # Dynamic runner image: constructed from caller identity so cross-account
  # deploys pull from the customer's own ECR (not the platform account's).
  runner_image = var.runner_image != "" ? var.runner_image : "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${var.runner_image_repo}:${var.runner_image_tag}"
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
  aws_region        = var.aws_region
  runner_namespace  = var.runner_namespace

  security_scans_bucket_arn = try(data.terraform_remote_state.platform.outputs.security_scans_bucket_arn, "")
}

# =============================================================================
# Secrets Manager (GitHub App credentials)
# =============================================================================

module "secrets" {
  source = "./modules/secrets"

  environment = var.environment
  name_prefix = local.name_prefix
  github_org  = var.github_org
}

# =============================================================================
# Beads State (DynamoDB + S3 for issue tracking)
# =============================================================================

module "beads_state" {
  source = "./modules/beads-state"

  environment = var.environment
  name_prefix = local.name_prefix
  kms_key_arn = aws_kms_key.dynamodb.arn
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
  github_repo      = var.github_repo
  runner_role_arn  = module.runner_iam.runner_role_arn

  # Authenticate the runner scale set with the "dev" persona's GitHub App.
  # Persona-specific GitHub App tokens for agent operations are minted at
  # runtime from the other secrets (pm, ops).
  github_app_id_secret_name          = "adp/${var.github_org}/gh-app-dev-id"
  github_app_private_key_secret_name = "adp/${var.github_org}/gh-app-dev-key"
  github_app_installation_id         = var.github_app_dev_installation_id

  # Custom ADP runner image with CLI tools pre-baked (aws, kubectl, terraform,
  # helm, gh, docker, kaniko). Empty string = chart default.
  runner_image = local.runner_image

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

# =============================================================================
# Namespace-scoped EKS access (Issue #1204 — replaces cluster-admin)
# =============================================================================
# Replaced AmazonEKSClusterAdminPolicy with AmazonEKSEditPolicy scoped to
# specific namespaces. Fine-grained K8s RBAC is enforced via kubernetes_role
# resources in runner-rbac.tf.
# =============================================================================

resource "aws_eks_access_policy_association" "runner_edit" {
  cluster_name  = local.cluster_name
  principal_arn = module.runner_iam.runner_role_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy"

  access_scope {
    type       = "namespace"
    namespaces = ["adp-gateway", "adp-gateway-agents", "adp-agents", "arc-systems", "arc-runners", "agent-context", "keda"]
  }

  depends_on = [aws_eks_access_entry.runner]
}

# =============================================================================
# Public CFN Templates Bucket
# =============================================================================
# Hosts customer-facing CloudFormation templates for IAM role setup.
# Templates are published here and linked via "Launch Stack" URLs.
# =============================================================================

resource "aws_s3_bucket" "public_cfn" {
  count  = var.enable_public_cfn_bucket ? 1 : 0
  bucket = "adp-public-cfn"

  tags = {
    Name    = "adp-public-cfn"
    Purpose = "Customer-facing CloudFormation templates"
  }
}

resource "aws_s3_bucket_public_access_block" "public_cfn" {
  count  = var.enable_public_cfn_bucket ? 1 : 0
  bucket = aws_s3_bucket.public_cfn[0].id

  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "public_cfn" {
  count  = var.enable_public_cfn_bucket ? 1 : 0
  bucket = aws_s3_bucket.public_cfn[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "PublicReadCFNTemplates"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.public_cfn[0].arn}/*"
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.public_cfn]
}
