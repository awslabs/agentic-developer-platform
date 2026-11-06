variable "aws_account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "Select the verified hosting account."
  }
}
variable "aws_region" { type = string }
variable "name" {
  type = string
  validation {
    condition     = can(regex("^[a-zA-Z0-9_-]{1,48}$", var.name))
    error_message = "Choose a distinct name of at most 48 characters."
  }
}
variable "connection_role_name" { type = string }
variable "setup_fingerprint" {
  type    = string
  default = ""
  validation {
    condition     = var.setup_fingerprint == "" || can(regex("^[a-f0-9]{64}$", var.setup_fingerprint))
    error_message = "Use the fingerprint of the retained ADP create operation."
  }
}
variable "native_space_id" {
  type    = string
  default = ""
  validation {
    condition     = var.native_space_id == "" || can(regex("^[A-Za-z0-9-]+$", var.native_space_id))
    error_message = "Use the exact space returned by AWS."
  }
}
variable "lambda_arn" { type = string }
variable "log_group_arn" { type = string }
