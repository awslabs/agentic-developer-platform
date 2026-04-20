# =============================================================================
# RDS Bootstrap Module — Variables
# =============================================================================
# One-shot Kubernetes Job that runs `GRANT rds_iam TO <user>` on fresh DBs.
# =============================================================================

variable "name_prefix" {
  type        = string
  description = "Resource name prefix (e.g. bedrockgw-dev)"
}

variable "namespace" {
  type        = string
  description = "Kubernetes namespace for the bootstrap Job"
  default     = "adp-gateway"
}

variable "aws_region" {
  type        = string
  description = "AWS region where RDS and Secrets Manager reside"
}

variable "db_host" {
  type        = string
  description = "RDS instance hostname (address, not endpoint)"
}

variable "db_name" {
  type        = string
  description = "Database name to connect to"
}

variable "db_username" {
  type        = string
  description = "Master username for RDS (the user to GRANT rds_iam to)"
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
