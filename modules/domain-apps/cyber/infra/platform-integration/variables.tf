variable "name_prefix" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "environment" {
  type = string
}

variable "account_id" {
  type = string
}

variable "namespace" {
  type = string
}

variable "oidc_provider_arn" {
  type = string
}

variable "oidc_issuer" {
  type = string
}

variable "worker_role_name" {
  type = string
}

variable "broker_image" {
  type        = string
  default     = ""
  description = "Override with the separately built cyber browser image digest after verifying it. Empty preserves the recorded deployed image."
}

variable "session_owner_routing" {
  type        = bool
  default     = false
  description = "Enable with matching new broker and worker images: balance new sessions and route existing capabilities to their owning pod."
}

variable "browser_mode" {
  type        = string
  default     = "broker"
  description = "Use native for direct AgentCore Browser after the protected worker image and IAM are ready."
  validation {
    condition     = contains(["native", "broker"], var.browser_mode)
    error_message = "browser_mode must be native or broker."
  }
}

variable "browser_broker_enabled" {
  type        = bool
  default     = true
  description = "Keep the legacy broker running until existing sessions drain; disable after native acceptance."
}
