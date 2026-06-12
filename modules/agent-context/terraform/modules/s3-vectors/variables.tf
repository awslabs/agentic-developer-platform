variable "environment" {
  description = "Environment (dev, staging, prod)"
  type        = string
}

variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
}

variable "account_id" {
  description = "AWS account ID"
  type        = string
}

variable "aws_region" {
  description = "AWS region for S3 Vectors"
  type        = string
}

variable "shard_count" {
  description = "Number of code-vector index shards (hash-by-org)"
  type        = number
  default     = 4
}

variable "dimension" {
  description = "Vector dimension (Titan Embed v2 = 1024)"
  type        = number
  default     = 1024
}

variable "distance_metric" {
  description = "Distance metric for vector similarity (cosine or euclidean)"
  type        = string
  default     = "cosine"
}

variable "irsa_role_name" {
  description = "Name of the IRSA role to attach S3 Vectors permissions to"
  type        = string
}
