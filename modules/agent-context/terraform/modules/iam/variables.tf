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
  description = "AWS region"
  type        = string
}

variable "oidc_provider_arn" {
  description = "EKS OIDC provider ARN"
  type        = string
}

variable "oidc_issuer" {
  description = "EKS OIDC issuer URL (without https:// prefix)"
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace for IRSA binding"
  type        = string
  default     = "agent-context"
}

variable "service_account" {
  description = "Kubernetes service account name for IRSA binding"
  type        = string
  default     = "agent-context-sa"
}

variable "bucket_name" {
  description = "S3 bucket name for platform data"
  type        = string
}

variable "rds_username" {
  description = "RDS username for IAM auth (rds-db:connect)"
  type        = string
  default     = "agent_context_svc"
}

variable "graphrag_enabled" {
  description = "Enable GraphRAG-related IAM policies (Neptune + OpenSearch Serverless)"
  type        = bool
  default     = false
}
