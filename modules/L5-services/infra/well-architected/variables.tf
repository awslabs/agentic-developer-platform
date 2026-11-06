variable "environment" { type = string }
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "db_security_group_id" { type = string }
variable "endpoint_security_group_id" { type = string }
variable "db_port" { type = number }
variable "permissions_boundary_arn" {
  type     = string
  nullable = false
  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:policy/[A-Za-z0-9_/-]+$", var.permissions_boundary_arn))
    error_message = "An approved IAM permissions boundary is required for L5 workers."
  }
}

variable "worker_artifact_bucket" {
  type = string
  validation {
    condition     = length(var.worker_artifact_bucket) > 0
    error_message = "Publish the worker archive before enabling L5."
  }
}
variable "worker_artifact_key" {
  type = string
  validation {
    condition     = length(var.worker_artifact_key) > 0
    error_message = "Pin the worker archive object key."
  }
}
variable "worker_artifact_version" {
  type = string
  validation {
    condition     = length(var.worker_artifact_version) > 0
    error_message = "Pin the worker archive object version."
  }
}
variable "worker_artifact_sha256" {
  type = string
  validation {
    condition     = can(regex("^[A-Za-z0-9+/]{43}=$", var.worker_artifact_sha256))
    error_message = "Provide the archive's base64-encoded SHA-256 digest."
  }
}
variable "worker_factory" {
  type    = string
  default = "l5_services.runtime_well_architected:factory"
  validation {
    condition     = can(regex("^[a-zA-Z_][a-zA-Z_0-9.]*:[a-zA-Z_][a-zA-Z_0-9]*$", var.worker_factory))
    error_message = "Provide an installed module:factory worker entry point."
  }
}

variable "worker_environment" {
  type    = map(string)
  default = {}
}

variable "gateway_api_arn" {
  type = string
  validation {
    condition     = can(regex("^arn:aws[a-z-]*:execute-api:[a-z0-9-]+:[0-9]{12}:[a-z0-9]+/\\*/\\*/internal/\\*$", var.gateway_api_arn))
    error_message = "A scoped Gateway internal API invoke ARN is required."
  }
}

variable "rds_connect_arn" {
  type = string
  validation {
    condition     = can(regex("^arn:aws[a-z-]*:rds-db:[a-z0-9-]+:[0-9]{12}:dbuser:[A-Za-z0-9-]+/[A-Za-z0-9_]+$", var.rds_connect_arn))
    error_message = "A scoped RDS IAM database user ARN is required."
  }
}


variable "agent_registry_table_name" { type = string }
