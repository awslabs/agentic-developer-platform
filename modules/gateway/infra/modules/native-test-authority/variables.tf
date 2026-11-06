variable "table_name" {
  type        = string
  description = "Dedicated native-test authority table, supplied by protected deployment configuration."
  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]{3,255}$", var.table_name))
    error_message = "A valid explicit DynamoDB table name is required."
  }
}

variable "gateway_role_arn" {
  type        = string
  description = "Exact gateway role; attach gateway_policy_json separately through protected deployment."
  validation {
    condition     = can(regex("^arn:aws(-us-gov|-cn)?:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$", var.gateway_role_arn))
    error_message = "An exact IAM role ARN without wildcard characters is required."
  }
}

variable "execution_table_arn" {
  type        = string
  default     = ""
  description = "Optional exact facade execution table for original cleanup proof reads."
  validation {
    condition     = var.execution_table_arn == "" || can(regex("^arn:aws:dynamodb:[a-z0-9-]+:[0-9]{12}:table/[A-Za-z0-9_.-]{3,255}$", var.execution_table_arn))
    error_message = "Supply one exact execution table ARN without wildcards."
  }
}

variable "registration_role_arn" {
  type        = string
  description = "Separate protected operator role authorized to retain/revoke fixture and deployment registration."
  validation {
    condition     = can(regex("^arn:aws(-us-gov|-cn)?:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$", var.registration_role_arn))
    error_message = "An exact IAM role ARN without wildcard characters is required."
  }
}

variable "ci_role_arns" {
  type        = list(string)
  description = "Every native-test CI role; none may read or mutate authority records directly."
  validation {
    condition     = length(var.ci_role_arns) > 0 && alltrue([for arn in var.ci_role_arns : can(regex("^arn:aws(-us-gov|-cn)?:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$", arn))])
    error_message = "At least one exact CI role ARN without wildcard characters is required."
  }
}
