variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must name the reviewed AWS account."
  }
}
variable "aws_region" {
  type = string
  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.aws_region))
    error_message = "aws_region must name a concrete AWS region."
  }
}
variable "environment" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,15}$", var.environment))
    error_message = "environment must be the reviewed workspace environment."
  }
}
variable "org_id" {
  type = string
  validation {
    condition     = can(regex("^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", var.org_id))
    error_message = "org_id must be the immutable domain organization UUID."
  }
}
variable "workspace_id" {
  type = string
  validation {
    condition     = can(regex("^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", var.workspace_id))
    error_message = "workspace_id must come from the actual workspace draft; display names are insufficient."
  }
}
variable "provider_role_arn" {
  type = string
  validation {
    condition     = can(regex("^arn:aws:iam::${var.account_id}:role/[A-Za-z0-9+=,.@_/-]+$", var.provider_role_arn))
    error_message = "provider_role_arn must name the exact selected role in the reviewed account."
  }
}
variable "expected_provider_role_id" {
  type = string
  validation {
    condition     = can(regex("^AROA[A-Z0-9]+$", var.expected_provider_role_id))
    error_message = "expected_provider_role_id must be the independently observed immutable IAM RoleId."
  }
}
variable "actor_role_names" {
  type = object({ registrar = string, installer = string, supervisor = string })
  validation {
    condition = (
      length(toset(values(var.actor_role_names))) == 3 &&
      alltrue([for name in values(var.actor_role_names) : can(regex("^[A-Za-z0-9+=,.@_-]{1,64}$", name)) && name != basename(var.provider_role_arn)])
    )
    error_message = "Provide three distinct new actor role names, none equal to the provider role."
  }
}
