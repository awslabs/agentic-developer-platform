# =============================================================================
# Agent Context Intelligence Platform Infrastructure
# =============================================================================
# Module-specific resources that layer on top of the shared platform infra.
# Uses remote state to reference the shared VPC, EKS cluster, and ECR.
# =============================================================================

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = merge(var.tags, {
      Project     = "agent-context-platform"
      Environment = var.environment
      ManagedBy   = "terraform"
    })
  }
}

# =============================================================================
# Shared Platform Remote State
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
  cluster_name           = data.terraform_remote_state.platform.outputs.eks_cluster_name
  vpc_id                 = data.terraform_remote_state.platform.outputs.vpc_id
  private_subnets        = data.terraform_remote_state.platform.outputs.private_subnet_ids
  node_security_group_id = data.terraform_remote_state.platform.outputs.eks_cluster_security_group_id
  oidc_issuer            = data.terraform_remote_state.platform.outputs.eks_oidc_issuer
  oidc_provider_arn      = data.terraform_remote_state.platform.outputs.eks_oidc_provider_arn
  cluster_endpoint       = data.terraform_remote_state.platform.outputs.eks_cluster_endpoint
  cluster_ca             = data.terraform_remote_state.platform.outputs.eks_cluster_ca_certificate

  name_prefix = "adp-${var.environment}-agent-context"
  bucket_name = "agent-context-platform-data-${var.account_id}"

  # RDS details from gateway module (shared instance) — only available when rds_enabled=true
  rds_host              = var.rds_enabled ? data.terraform_remote_state.gateway[0].outputs.rds_instance_address : ""
  rds_instance_id       = var.rds_enabled ? data.terraform_remote_state.gateway[0].outputs.rds_instance_id : ""
  rds_master_secret_arn = var.rds_enabled ? data.terraform_remote_state.gateway[0].outputs.rds_master_user_secret_arn : ""
}

# =============================================================================
# Gateway Remote State (for RDS instance details)
# =============================================================================
# The agent_context database lives on the gateway's shared RDS instance.
# We read its outputs to get the host, instance ID, and master secret ARN.
# Conditional: only read if rds_enabled=true (gateway must be deployed first).

data "terraform_remote_state" "gateway" {
  count   = var.rds_enabled ? 1 : 0
  backend = "s3"
  config = {
    bucket = "adp-terraform-state-${var.account_id}"
    key    = "${var.environment}/gateway/terraform.tfstate"
    region = var.aws_region
  }
}

# =============================================================================
# Kubernetes Provider (for bootstrap Job)
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

# =============================================================================
# Look up the EKS cluster for OIDC provider URL
# =============================================================================

data "aws_eks_cluster" "this" {
  name = local.cluster_name
}

# =============================================================================
# IAM (IRSA role for agent-context service account)
# =============================================================================

module "iam" {
  source = "./modules/iam"

  environment       = var.environment
  name_prefix       = local.name_prefix
  account_id        = data.aws_caller_identity.current.account_id
  aws_region        = data.aws_region.current.name
  oidc_provider_arn = local.oidc_provider_arn
  oidc_issuer       = local.oidc_issuer
  namespace         = var.namespace
  service_account   = var.service_account
  bucket_name       = local.bucket_name
  rds_username      = "agent_context_svc"
  graphrag_enabled  = var.graphrag_enabled
}

# =============================================================================
# S3 Files Storage (S3 bucket + Mountpoint for Amazon S3 CSI driver)
# =============================================================================

module "s3_files" {
  source = "./modules/s3-files"

  cluster_name      = local.cluster_name
  aws_region        = var.aws_region
  bucket_name       = local.bucket_name
  namespace         = var.namespace
  oidc_provider_url = data.aws_eks_cluster.this.identity[0].oidc[0].issuer
  tags              = var.tags
}

# =============================================================================
# Neptune Serverless (GraphRAG knowledge graph)
# =============================================================================

module "neptune_serverless" {
  source = "./modules/neptune-serverless"
  count  = var.graphrag_enabled ? 1 : 0

  cluster_name           = local.cluster_name
  aws_region             = var.aws_region
  namespace              = var.namespace
  vpc_id                 = local.vpc_id
  subnet_ids             = local.private_subnets
  node_security_group_id = local.node_security_group_id
  oidc_provider_url      = data.aws_eks_cluster.this.identity[0].oidc[0].issuer
  min_capacity           = var.neptune_min_capacity
  max_capacity           = var.neptune_max_capacity
  tags                   = var.tags
}

# =============================================================================
# OpenSearch Serverless (GraphRAG entity embeddings)
# =============================================================================

module "opensearch_serverless" {
  source = "./modules/opensearch-serverless"
  count  = var.graphrag_enabled ? 1 : 0

  cluster_name        = local.cluster_name
  aws_region          = var.aws_region
  namespace           = var.namespace
  collection_name     = var.opensearch_collection_name
  oidc_provider_url   = data.aws_eks_cluster.this.identity[0].oidc[0].issuer
  allow_public_access = var.opensearch_allow_public_access
  tags                = var.tags
}

# =============================================================================
# SQS Ingestion Queue (parallel ingestion pipeline)
# =============================================================================

module "sqs_ingestion" {
  source = "./modules/sqs-ingestion"

  cluster_name   = local.cluster_name
  irsa_role_name = module.iam.role_name
  tags           = var.tags

  depends_on = [module.iam]
}

# =============================================================================
# DynamoDB State Table (ingestion state tracking)
# =============================================================================

module "dynamodb_state" {
  source = "./modules/dynamodb-state"

  table_name     = var.dynamodb_table_name
  irsa_role_name = module.iam.role_name
  kms_key_arn    = aws_kms_key.dynamodb.arn
  tags           = var.tags

  depends_on = [module.iam]
}

# =============================================================================
# S3 Vectors (semantic search — code embeddings + personal context)
# =============================================================================

module "s3_vectors" {
  source = "./modules/s3-vectors"

  environment     = var.environment
  name_prefix     = local.name_prefix
  account_id      = data.aws_caller_identity.current.account_id
  aws_region      = data.aws_region.current.name
  shard_count     = var.s3_vectors_shard_count
  dimension       = 1024
  distance_metric = "cosine"
  irsa_role_name  = module.iam.role_name

  depends_on = [module.iam]
}

# =============================================================================
# Docker Images Build (ECR repos + CodeBuild projects for auto-rebuild)
# =============================================================================

module "images_build" {
  source = "./modules/images-build"

  environment                = var.environment
  aws_region                 = var.aws_region
  name_prefix                = local.name_prefix
  state_bucket               = "adp-terraform-state-${data.aws_caller_identity.current.account_id}"
  codebuild_service_role_arn = data.terraform_remote_state.platform.outputs.codebuild_role_arn
  common_tags                = var.tags
}

# =============================================================================
# RDS Bootstrap (agent_context database + user on shared gateway RDS instance)
# =============================================================================
# Creates the `agent_context` database and `agent_context_svc` role on the
# gateway's shared RDS instance. Issue #1355.

module "rds_bootstrap" {
  source = "./modules/rds-bootstrap"
  count  = var.rds_enabled ? 1 : 0

  name_prefix            = local.name_prefix
  namespace              = var.namespace
  aws_region             = var.aws_region
  db_host                = local.rds_host
  master_user_secret_arn = local.rds_master_secret_arn
  oidc_provider_arn      = local.oidc_provider_arn
  oidc_issuer            = local.oidc_issuer
  rds_instance_id        = local.rds_instance_id
  common_tags            = var.tags
}

# =============================================================================
# SSM Parameter: RDS Endpoint (Issue #1437)
# =============================================================================
# Publishes the RDS host to SSM so the deploy workflow's migration Job can
# discover it without needing local Terraform state. The deploy workflow reads
# /adp/<env>/rds/endpoint; this makes that reliable.

resource "aws_ssm_parameter" "rds_endpoint" {
  count = var.rds_enabled ? 1 : 0

  name  = "/adp/${var.environment}/rds/endpoint"
  type  = "String"
  value = local.rds_host

  description = "RDS endpoint for agent-context migrations and workloads"

  tags = merge(var.tags, {
    ManagedBy = "terraform"
    Module    = "agent-context"
  })
}
