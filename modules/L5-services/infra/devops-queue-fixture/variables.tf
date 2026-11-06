variable "aws_account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "Select one fixture account."
  }
}
variable "aws_region" { type = string }
variable "fixture_id" {
  type = string
  validation {
    condition     = can(regex("^[a-f0-9]{16}$", var.fixture_id))
    error_message = "Use the prepared fixture identity."
  }
}
variable "worker_zip" { type = string }
variable "worker_sha256_base64" {
  type = string
  validation {
    condition     = can(regex("^[A-Za-z0-9+/]{43}=$", var.worker_sha256_base64))
    error_message = "Use the pinned deterministic ZIP digest."
  }
}
variable "source_commit" {
  type = string
  validation {
    condition     = can(regex("^[a-f0-9]{40}$", var.source_commit))
    error_message = "Source provenance requires an immutable full Git SHA."
  }
}
variable "phase" {
  type    = string
  default = "sealed"
  validation {
    condition     = contains(["bootstrap", "sealed", "active"], var.phase)
    error_message = "Explicitly select bootstrap, sealed or active."
  }
}
