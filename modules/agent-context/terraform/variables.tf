variable "cluster_name" {
  description = "Name of the EKS cluster"
  type        = string
  default     = "github-arc-runner-eks"
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "bucket_name" {
  description = "S3 bucket name for platform data"
  type        = string
  default     = "agent-context-platform-data"
}

variable "namespace" {
  description = "Kubernetes namespace for the platform"
  type        = string
  default     = "agent-context"
}

variable "vpc_id" {
  description = "VPC ID of the EKS cluster"
  type        = string
}

variable "subnet_ids" {
  description = "Subnet IDs for mount targets (same as EKS node subnets)"
  type        = list(string)
}

variable "node_security_group_id" {
  description = "Security group ID of EKS nodes"
  type        = string
}

variable "tags" {
  description = "Additional tags to apply to all resources"
  type        = map(string)
  default     = {}
}

# ─── GraphRAG (Neptune + OpenSearch Serverless) ──────────────────────────────

variable "graphrag_enabled" {
  description = "Enable GraphRAG infrastructure (Neptune Serverless + OpenSearch Serverless)"
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
  description = "Allow public access to OpenSearch Serverless (set false for VPC-only, recommended for production)"
  type        = bool
  default     = false
}

# ─── SQS + DynamoDB (Parallel Ingestion Pipeline) ────────────────────────

variable "irsa_role_name" {
  description = "IRSA role name to attach SQS/DynamoDB policies to"
  type        = string
  default     = "agent-context-platform-irsa"
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table for ingestion state"
  type        = string
  default     = "adp-context-service-state"
}
