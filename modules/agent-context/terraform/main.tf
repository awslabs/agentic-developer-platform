provider "aws" {
  region = var.aws_region

  default_tags {
    tags = merge(var.tags, {
      Project   = "agent-context-platform"
      ManagedBy = "terraform"
    })
  }
}

# Look up the EKS cluster for OIDC provider
data "aws_eks_cluster" "this" {
  name = var.cluster_name
}

module "s3_files" {
  source = "./modules/s3-files"

  cluster_name           = var.cluster_name
  aws_region             = var.aws_region
  bucket_name            = var.bucket_name
  namespace              = var.namespace
  vpc_id                 = var.vpc_id
  subnet_ids             = var.subnet_ids
  node_security_group_id = var.node_security_group_id
  oidc_provider_url      = data.aws_eks_cluster.this.identity[0].oidc[0].issuer
  tags                   = var.tags
}

# ─── Neptune Serverless (GraphRAG knowledge graph) ─────────────────────────

module "neptune_serverless" {
  source = "./modules/neptune-serverless"
  count  = var.graphrag_enabled ? 1 : 0

  cluster_name           = var.cluster_name
  aws_region             = var.aws_region
  namespace              = var.namespace
  vpc_id                 = var.vpc_id
  subnet_ids             = var.subnet_ids
  node_security_group_id = var.node_security_group_id
  oidc_provider_url      = data.aws_eks_cluster.this.identity[0].oidc[0].issuer
  min_capacity           = var.neptune_min_capacity
  max_capacity           = var.neptune_max_capacity
  tags                   = var.tags
}

# ─── OpenSearch Serverless (GraphRAG entity embeddings) ────────────────────

module "opensearch_serverless" {
  source = "./modules/opensearch-serverless"
  count  = var.graphrag_enabled ? 1 : 0

  cluster_name       = var.cluster_name
  aws_region         = var.aws_region
  namespace          = var.namespace
  collection_name    = var.opensearch_collection_name
  oidc_provider_url  = data.aws_eks_cluster.this.identity[0].oidc[0].issuer
  allow_public_access = var.opensearch_allow_public_access
  tags               = var.tags
}

# ─── SQS Ingestion Queue (parallel ingestion pipeline) ────────────────────

module "sqs_ingestion" {
  source = "./modules/sqs-ingestion"

  cluster_name   = var.cluster_name
  irsa_role_name = var.irsa_role_name
  tags           = var.tags
}

# ─── DynamoDB State Table (ingestion state tracking) ──────────────────────

module "dynamodb_state" {
  source = "./modules/dynamodb-state"

  table_name     = var.dynamodb_table_name
  irsa_role_name = var.irsa_role_name
  tags           = var.tags
}
