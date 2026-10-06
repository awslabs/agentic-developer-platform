variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "An exact AWS account is required."
  }
}
variable "aws_region" {
  type = string
  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.aws_region))
    error_message = "An exact AWS region is required."
  }
}
variable "environment" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,9}$", var.environment))
    error_message = "An explicit workspace environment is required."
  }
}
variable "installation_id" {
  type = string
  validation {
    condition     = can(regex("^[a-f0-9]{24}$", var.installation_id))
    error_message = "Use the exact maintained installation identity."
  }
}
variable "org_id" {
  type = string
  validation {
    condition     = can(regex("^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", var.org_id))
    error_message = "An immutable domain organization UUID is required."
  }
}
variable "workspace_id" {
  type = string
  validation {
    condition     = can(regex("^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", var.workspace_id))
    error_message = "An immutable request-derived workspace UUID is required."
  }
}
variable "gateway_role_arn" {
  type = string
  validation {
    condition     = can(regex("^arn:aws:iam::${var.account_id}:role/[A-Za-z0-9+=,.@_-]+$", var.gateway_role_arn))
    error_message = "Name the exact same-account Gateway role."
  }
}
variable "gateway_role_id" {
  type = string
  validation {
    condition     = can(regex("^AROA[A-Z0-9]{16,32}$", var.gateway_role_id))
    error_message = "An independently observed Gateway RoleId is required."
  }
}
variable "beneficiary_user_id" {
  type = string
  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,128}$", var.beneficiary_user_id))
    error_message = "An explicit canonical beneficiary selector is required."
  }
}
variable "external_id" {
  type      = string
  sensitive = true
  validation {
    condition     = can(regex("^[A-Za-z0-9+=,.@:/_-]{32,255}$", var.external_id))
    error_message = "Supply a private installation-specific ExternalId of at least 32 characters."
  }
}
variable "node_image_repository_arns" {
  type = set(string)
  validation {
    condition = length(var.node_image_repository_arns) > 0 && alltrue([
      for arn in var.node_image_repository_arns : can(regex("^arn:aws:ecr:${var.aws_region}:[0-9]{12}:repository/[a-z0-9][a-z0-9._/-]*$", arn))
    ])
    error_message = "Supply all exact reviewed ECR repositories, including pinned EKS system repositories."
  }
}
variable "execution" {
  description = "Null prepares only inert provider identity/secret/boundary. A separately reviewed second plan supplies real retained foundation and backend identities before enrollment."
  type = object({
    kms_key_arn                   = string
    actor_role_arns               = set(string)
    state_bucket                  = string
    lock_table                    = string
    validation_image_id           = string
    validation_instance_type      = string
    validation_subnet_id          = string
    validation_security_group_ids = set(string)
  })
  default = null
  validation {
    condition = var.execution == null ? true : (
      can(regex("^arn:aws:kms:${var.aws_region}:${var.account_id}:key/[a-f0-9-]{36}$", var.execution.kms_key_arn)) &&
      length(var.execution.actor_role_arns) == 3 && alltrue([for arn in var.execution.actor_role_arns : can(regex("^arn:aws:iam::${var.account_id}:role/[A-Za-z0-9+=,.@_-]+$", arn))]) &&
      can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.execution.state_bucket)) &&
      can(regex("^[A-Za-z0-9_.-]{3,255}$", var.execution.lock_table)) &&
      can(regex("^ami-[0-9a-f]+$", var.execution.validation_image_id)) &&
      can(regex("^[a-z][a-z0-9-]*\\.[a-z0-9]+$", var.execution.validation_instance_type)) &&
      can(regex("^subnet-[0-9a-f]+$", var.execution.validation_subnet_id)) &&
      length(var.execution.validation_security_group_ids) > 0 &&
      alltrue([for id in var.execution.validation_security_group_ids : can(regex("^sg-[0-9a-f]+$", id))])
    )
    error_message = "Execution requires exact same-account foundation key/three actor roles, backend and reviewed EC2 validation profile."
  }
}
