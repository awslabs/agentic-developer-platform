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
