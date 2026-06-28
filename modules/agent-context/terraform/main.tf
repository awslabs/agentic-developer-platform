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

# ---------------------------------------------------------------------------
# Neptune subnet selection (multi-AZ, capacity-aware)
#
# The platform's private_subnet_ids output can include subnets that are
# IP-exhausted (e.g. EKS-consumed) or that collapse to a single AZ. Neptune
# requires a subnet group spanning >= 2 AZs with free IPs. Discover ALL private
# subnets in the VPC that have available IPs, then pick one per AZ so the
# subnet group always satisfies the multi-AZ requirement.
# ---------------------------------------------------------------------------
data "aws_subnets" "private_all" {
  count = var.neptune_enabled ? 1 : 0

  filter {
    name   = "vpc-id"
    values = [local.vpc_id]
  }
  filter {
    name   = "tag:kubernetes.io/role/internal-elb"
    values = ["1"]
  }
}

data "aws_subnet" "neptune_candidates" {
  for_each = var.neptune_enabled ? toset(data.aws_subnets.private_all[0].ids) : toset([])
  id       = each.value
}

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

  # Neptune subnet selection: from all private subnets in the VPC, keep only
  # those with free IPs, then pick ONE per AZ. Guarantees the subnet group
  # spans every AZ that has capacity — satisfying Neptune's >= 2-AZ requirement
  # even when the platform's default subnet list is single-AZ or IP-exhausted.
  _neptune_subnets_with_ips = var.neptune_enabled ? {
    for s in data.aws_subnet.neptune_candidates : s.availability_zone => s.id...
    if s.available_ip_address_count > 0
  } : {}

  # First subnet id per AZ (one per AZ).
  neptune_subnet_ids = [for az, ids in local._neptune_subnets_with_ips : ids[0]]
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
  neptune_enabled   = var.neptune_enabled

  # Allow the KEDA operator to chain-assume this IRSA role for SQS queue-depth
  # polling of the ingestion ScaledJob (#2213).
  keda_operator_role_arn = data.aws_iam_role.keda_operator.arn
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
  count  = var.neptune_enabled ? 1 : 0

  cluster_name           = local.cluster_name
  aws_region             = var.aws_region
  namespace              = var.namespace
  vpc_id                 = local.vpc_id
  subnet_ids             = local.neptune_subnet_ids
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
# Observability (Knowledge Layer dashboard + alarms)
# =============================================================================
# Issue: #1757 — CloudWatch dashboard, log groups, alarms, and SNS topic for
# the Knowledge Layer pipeline. Gated by enable_knowledge_layer_otel.

module "observability" {
  source = "./modules/observability"
  count  = var.enable_knowledge_layer_otel ? 1 : 0

  environment           = var.environment
  aws_region            = var.aws_region
  name_prefix           = local.name_prefix
  sqs_queue_name_prefix = "${local.cluster_name}-context"
  tags                  = var.tags
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

# =============================================================================
# SSM Parameter: Ingestion Queue URL (Issue #2213)
# =============================================================================
# Publishes the SQS ingestion queue URL to SSM so both consumers can discover
# it without needing local Terraform state:
#   - agent-context-deploy.yml reads it → sets SQS_QUEUE_URL → deploys ScaledJob
#   - gateway-deploy.yml reads it → sets INGESTION_QUEUE_URL → enables dispatch
# Single source of truth: module.sqs_ingestion.queue_url → this SSM param.

resource "aws_ssm_parameter" "ingestion_queue_url" {
  name  = "/adp/${var.environment}/agent-context/ingestion-queue-url"
  type  = "String"
  value = module.sqs_ingestion.queue_url

  description = "SQS ingestion queue URL for Knowledge Layer pipeline (producer + consumer)"

  tags = merge(var.tags, {
    ManagedBy = "terraform"
    Module    = "agent-context"
  })
}

# =============================================================================
# KEDA operator SQS-scaler access for the ingestion ScaledJob (Issue #2213)
# =============================================================================
# KEDA's aws-eks pod-identity provider authenticates as the keda-operator SA
# first, then chain-assumes the workload IRSA role to call SQS
# GetQueueAttributes for queue-depth scaling. Without this the ingestion
# ScaledJob fails with AccessDenied (sts:AssumeRole on the IRSA role) and
# never scales. Mirrors the gateway pattern in
# modules/agent-factory/infra/gateway-main.tf ("gateway-sqs-scaler-read").
#
# The keda-operator-role is owned by Phase 7
# (modules/agent-factory/webhook-ingress/infra/keda.tf); referenced here
# read-only via a data source. The reciprocal trust statement allowing the
# operator to assume the IRSA role lives in modules/iam (agent_context role).

data "aws_iam_role" "keda_operator" {
  name = "adp-${var.environment}-keda-operator-role"
}

resource "aws_iam_role_policy" "keda_operator_ingestion_sqs" {
  name = "agent-context-ingestion-sqs-scaler-read"
  role = data.aws_iam_role.keda_operator.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SQSQueuePolling"
        Effect = "Allow"
        Action = [
          "sqs:GetQueueAttributes",
          "sqs:ListQueues",
        ]
        Resource = [
          module.sqs_ingestion.queue_arn,
        ]
      },
      {
        Sid      = "AssumeWorkloadRole"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = module.iam.role_arn
      }
    ]
  })
}
