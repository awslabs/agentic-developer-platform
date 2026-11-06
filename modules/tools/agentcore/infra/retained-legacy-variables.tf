variable "retain_legacy_routes" {
  description = "Retain existing retired REST resources in their original shared state; never enable for a new installation."
  type        = bool
  default     = false
}

variable "retain_legacy_api_routes" {
  description = "Keep historical API resources when retaining legacy state. Disable only after independently verifying all three obsolete API routes are already absent; never use this to delete live routes."
  type        = bool
  default     = true
}

variable "tools_parent_resource_id" {
  description = "Existing REST tools parent identity for retained routes, supplied privately."
  type        = string
  default     = ""
}

variable "worker_role_arns" {
  description = "Exact historical worker role ARN keys in the existing shared state, supplied privately."
  type        = set(string)
  default     = []
  validation {
    condition = alltrue([for arn in var.worker_role_arns :
      can(regex("^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$", arn)) &&
      startswith(arn, "arn:aws:iam::${var.aws_account_id}:role/")
    ])
    error_message = "Retained worker policy keys must be exact role ARNs in the confirmed account."
  }
}
