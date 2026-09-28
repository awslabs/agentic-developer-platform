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

variable "tools_endpoint" {
  type        = string
  default     = ""
  description = "Optional exact AWS_IAM cyber tools HTTPS route; empty leaves Task cyber calls unconfigured."
  validation {
    condition     = var.tools_endpoint == "" || can(regex("^https://[A-Za-z0-9.-]+(:443)?(/[A-Za-z0-9_-]+)*/tools/cyber$", var.tools_endpoint))
    error_message = "tools_endpoint must be an HTTPS /tools/cyber endpoint without credentials, query, fragment or trailing slash."
  }
}

variable "task_url_tools_enabled" {
  type        = bool
  default     = false
  description = "Enable URL tool routing after the Lambda and native Task worker pass acceptance."
}

variable "websearch_enabled" {
  description = "Publish the IAM Web Search Task route only after qualification. Existing browser routes remain active."
  type        = bool
  default     = false
  validation {
    condition     = !var.websearch_enabled || var.tools_endpoint != ""
    error_message = "Enabling Web Search requires the existing cyber tools endpoint."
  }
}
