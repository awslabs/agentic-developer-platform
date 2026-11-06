variable "aws_account_id" {
  type        = string
  description = "Confirmed account in which the caller provisions the image publisher."
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "A confirmed 12-digit account is required."
  }
}

variable "aws_region" {
  type = string
  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9]+$", var.aws_region))
    error_message = "A supported AWS region is required."
  }
}

variable "environment" {
  type = string
  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "The tools release environment must be dev or prod."
  }
}

variable "state_bucket" {
  type        = string
  description = "Existing private source archive bucket; this module grants reads only in its own project prefix."
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.state_bucket))
    error_message = "A source bucket name is required."
  }
}

variable "source_kms_key_arn" {
  type        = string
  default     = ""
  description = "Optional exact key for an SSE-KMS source bucket. Empty for SSE-S3; never an app-secret key."
  validation {
    condition     = var.source_kms_key_arn == "" || can(regex("^arn:aws:kms:${var.aws_region}:${var.aws_account_id}:key/[0-9a-f-]+$", var.source_kms_key_arn))
    error_message = "The source key must belong to the selected account and region."
  }
}
