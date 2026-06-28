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
  description = "Enable OpenSearch Serverless IAM policy (full GraphRAG). Neptune IAM is gated separately via neptune_enabled."
  type        = bool
  default     = false
}

variable "neptune_enabled" {
  description = "Enable the Neptune IAM policy independently of OpenSearch/GraphRAG."
  type        = bool
  default     = false
}

variable "keda_operator_role_arn" {
  description = "ARN of the KEDA operator role allowed to chain-assume this IRSA role for SQS scaling. Empty disables the trust statement. Issue #2213."
  type        = string
  default     = ""
}
