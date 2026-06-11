# =============================================================================
# Agent-Context RDS Bootstrap Module — Variables
# =============================================================================

variable "name_prefix" {
  type        = string
  description = "Resource name prefix (e.g. adp-dev-agent-context)"
}

variable "namespace" {
  type        = string
  description = "Kubernetes namespace for the bootstrap Job"
  default     = "agent-context"
}

variable "aws_region" {
  type        = string
  description = "AWS region where RDS and Secrets Manager reside"
}

variable "db_host" {
  type        = string
  description = "RDS instance hostname (the shared gateway instance)"
}

variable "gateway_db_name" {
  type        = string
  description = "Gateway database name (used as connection target for CREATE DATABASE)"
  default     = "bedrockgateway"
}

variable "ac_db_name" {
  type        = string
  description = "Name of the agent_context database to create"
  default     = "agent_context"
}

variable "ac_db_username" {
  type        = string
  description = "PostgreSQL role to create for agent-context workloads"
  default     = "agent_context_svc"
}

variable "master_user_secret_arn" {
  type        = string
  description = "ARN of the AWS-managed Secrets Manager secret holding the RDS master user password"
}

variable "oidc_provider_arn" {
  type        = string
  description = "ARN of the EKS OIDC provider for IRSA"
}

variable "oidc_issuer" {
  type        = string
  description = "OIDC issuer URL (without https://) for IRSA trust policy"
}

variable "common_tags" {
  type        = map(string)
  description = "Common tags to apply to resources"
  default     = {}
}

variable "rds_instance_id" {
  type        = string
  description = "RDS instance ID — used as a trigger so the Job re-runs when the DB is replaced"
}
