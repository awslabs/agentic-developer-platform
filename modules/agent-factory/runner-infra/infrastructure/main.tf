terraform {
  required_version = ">= 1.0"

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

  # Uncomment for remote state
  # backend "s3" {
  #   bucket = "your-terraform-state-bucket"
  #   key    = "github-actions-runner/terraform.tfstate"
  #   region = "us-east-1"
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "adp"
      Environment = var.environment
      Module      = "agent-factory"
      ManagedBy   = "terraform"
      Owner       = "agent-team"
      CostCenter  = "engineering"
    }
  }
}

# Kubernetes provider for namespace-scoped RBAC (Issue #1204)
provider "kubernetes" {
  host                   = aws_eks_cluster.main.endpoint
  cluster_ca_certificate = base64decode(aws_eks_cluster.main.certificate_authority[0].data)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", aws_eks_cluster.main.name]
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
