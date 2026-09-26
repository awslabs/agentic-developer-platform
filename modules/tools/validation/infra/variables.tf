variable "enabled" {
  type    = bool
  default = false
}
variable "capability_enabled" {
  type    = bool
  default = false
}
variable "name" {
  type    = string
  default = "adp-codex-validation"
}
variable "image_uri" {
  type    = string
  default = ""
  validation {
    condition     = var.image_uri == "" || can(regex("^[0-9]{12}\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com(\\.cn)?/[a-z0-9/_-]+@sha256:[a-f0-9]{64}$", var.image_uri))
    error_message = "Use an immutable ECR image digest."
  }
}
variable "api_execution_arn" { type = string }
variable "rest_api_id" { type = string }
variable "stage_name" { type = string }
variable "tools_parent_resource_id" { type = string }
variable "authority_endpoint" { type = string }
variable "worker_role_arns" { type = set(string) }
variable "cluster_name" { type = string }
variable "cluster_endpoint" { type = string }
variable "cluster_ca" { type = string }
variable "subnet_ids" { type = list(string) }
variable "security_group_ids" { type = list(string) }
variable "validation_namespace" {
  type    = string
  default = "adp-codex-validation"
}
variable "isolation_qualified" {
  type        = bool
  default     = false
  description = "Operator has verified deny-all network enforcement, UID/root isolation and kubelet podPidsLimit <= 128 on the labelled validation nodes."
}

variable "agent_registry_table_name" {
  type        = string
  default     = ""
  description = "Existing gateway service-account registry; service identity is provisioned with its role."
}
