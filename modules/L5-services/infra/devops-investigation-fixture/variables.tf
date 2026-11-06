variable "aws_account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "Select one fixture account."
  }
}
variable "aws_region" {
  type = string
  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[1-9]$", var.aws_region))
    error_message = "Select one fixture region."
  }
}
variable "fixture_id" {
  type = string
  validation {
    condition     = can(regex("^[a-f0-9]{16}$", var.fixture_id))
    error_message = "Use a distinct prepared fixture identity."
  }
}
variable "native_space_id" {
  type = string
  validation {
    condition     = can(regex("^[a-zA-Z0-9-]{1,64}$", var.native_space_id))
    error_message = "Supply the actual read-back native space ID; no wildcard is allowed."
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
    error_message = "Pin the fixture source to an immutable full Git SHA."
  }
}
variable "phase" {
  type    = string
  default = "sealed"
  validation {
    condition     = contains(["bootstrap", "sealed", "active"], var.phase)
    error_message = "Explicitly select bootstrap, sealed, or active."
  }
}
