variable "environment" {
  type = string
  validation {
    condition     = contains(["dev", "test", "prod"], var.environment)
    error_message = "Select a protected deployment environment."
  }
}
variable "aws_region" { type = string }
variable "aws_account_id" { type = string }
variable "github_repository" { type = string }
variable "github_environment" { type = string }
variable "state_bucket" { type = string }
variable "artifact_bucket" { type = string }
variable "agent_registry_table_name" { type = string }
variable "agent_registry_kms_key_arn" {
  description = "Optional customer-managed key encrypting the selected worker registry."
  type        = string
  default     = ""
  validation {
    condition     = var.agent_registry_kms_key_arn == "" || can(regex("^arn:[a-z-]+:kms:[a-z0-9-]+:[0-9]{12}:key/[a-f0-9-]+$", var.agent_registry_kms_key_arn))
    error_message = "Select the exact registry KMS key ARN, or leave it empty for an AWS-owned key."
  }
}
variable "gateway_role_name" { type = string }
variable "gateway_api_arn" { type = string }
variable "rds_connect_arn" { type = string }
variable "vpc_id" { type = string }
variable "platform_security_group_ids" { type = set(string) }

variable "transform_cluster_name" {
  description = "Optional selected EKS cluster for the private Transform executor; grants discovery only."
  type        = string
  default     = ""
  validation {
    condition     = var.transform_cluster_name == "" || can(regex("^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$", var.transform_cluster_name))
    error_message = "Select one EKS cluster name, or leave Transform cluster discovery disabled."
  }
}
