# =============================================================================
# Agent Context Platform — Variables
# =============================================================================

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment (dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "account_id" {
  description = "AWS account ID (used for remote state bucket name and resource naming)"
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace for the platform"
  type        = string
  default     = "agent-context"
}

variable "service_account" {
  description = "Kubernetes service account name for IRSA binding"
  type        = string
  default     = "agent-context-sa"
}

variable "tags" {
  description = "Additional tags to apply to all resources"
  type        = map(string)
  default     = {}
}

# --- GraphRAG (Neptune + OpenSearch Serverless) ------------------------------

variable "graphrag_enabled" {
  description = "Enable GraphRAG infrastructure (Neptune Serverless + OpenSearch Serverless). WARNING: expensive."
  type        = bool
  default     = false
}

variable "neptune_min_capacity" {
  description = "Minimum NCUs for Neptune Serverless"
  type        = number
  default     = 1.0
}

variable "neptune_max_capacity" {
  description = "Maximum NCUs for Neptune Serverless"
  type        = number
  default     = 16.0
}

variable "opensearch_collection_name" {
  description = "Name for the OpenSearch Serverless collection"
  type        = string
  default     = "graphrag-entities"
}

variable "opensearch_allow_public_access" {
  description = "Allow public access to OpenSearch Serverless (set false for VPC-only)"
  type        = bool
  default     = false
}

# --- S3 Vectors (Semantic Search) --------------------------------------------

variable "s3_vectors_shard_count" {
  description = "Number of code-vector index shards for write-throughput scaling (hash-by-org)"
  type        = number
  default     = 4
}

# --- SQS + DynamoDB (Parallel Ingestion Pipeline) ---------------------------

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table for ingestion state"
  type        = string
  default     = "adp-context-service-state"
}
