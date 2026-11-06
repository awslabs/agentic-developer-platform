variable "environment" {
  type = string
  validation {
    condition     = contains(["dev", "test", "prod"], var.environment)
    error_message = "Select a protected deployment environment."
  }
}

variable "aws_region" { type = string }
variable "aws_account_id" { type = string }

variable "gateway_role_name" {
  type = string
  validation {
    condition     = can(regex("^[A-Za-z0-9+=,.@_-]{1,64}$", var.gateway_role_name))
    error_message = "Select the existing Gateway role by its name."
  }
}

variable "archive_bucket" {
  description = "The same private versioned bucket selected as FinOps L5_ARCHIVE_BUCKET."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.archive_bucket))
    error_message = "Select one private archive bucket name."
  }
}
