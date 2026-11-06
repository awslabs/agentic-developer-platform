variable "aws_account_id" {
  type        = string
  description = "Confirmed account of the build infrastructure and reviewed EKS target."
}

variable "aws_region" {
  type        = string
  description = "Explicit AWS region used by both providers and EKS token acquisition."
}

variable "environment" {
  type        = string
  description = "Reviewed tools release environment (dev or prod)."
}

variable "state_bucket" {
  type        = string
  description = "Existing private versioned bucket for reviewed source and qualified build archives. Backend settings are supplied separately."
}

variable "source_kms_key_arn" {
  type        = string
  default     = ""
  description = "Optional exact SSE-KMS key for the archive bucket; empty for SSE-S3."
}

variable "cluster_name" {
  type        = string
  description = "Reviewed existing EKS cluster; its endpoint and CA bind the Kubernetes provider."
}

variable "namespace" {
  type        = string
  description = "Dedicated adapter namespace, matching the reviewed workload renderer input."
}

variable "oidc_provider_arn" {
  type        = string
  description = "Existing IAM OIDC provider for this exact EKS cluster."
}

variable "runtime_role_name" {
  type        = string
  description = "Dedicated adapter pod role name; foundation creates trust without provider grants."
}

variable "atlassian_mcp_identity_enabled" {
  type        = bool
  description = "Grant this adapter role only the isolated MCP user's Identity token actions after protected observation-mode qualification."
  default     = false
}

variable "private_edge_enabled" {
  type        = bool
  default     = false
  description = "Provision a separate internal adapter ingress after the adapter Service exists; does not activate connector routes."
}

variable "runtime_verifier_group" {
  type        = string
  description = "Existing environment connection's EKS verifier group."
}

variable "observation_runtime" {
  type = object({
    vpc_id                  = string
    subnet_ids              = set(string)
    core_security_group_ids = set(string)
    vpc_link_id             = string
    adapter_alb_arn         = string
    core_api_id             = string
    core_api_stage          = string
    core_role_arn           = string
    assertion_key_arn       = optional(string)
    registry_table          = string
    tenant_id               = string
    release = optional(object({
      contract_version   = string
      source_revision    = string
      image_digest       = string
      group              = string
      group_generation   = number
      backend_generation = number
    }))
  })
  default     = null
  description = "Confirmed runtime bindings; release stays null until a matching observation configuration and image have been independently verified."
}
