variable "selection" {
  description = "Required exact genuine case and independently read original ADP-created space. No source selection or consent is inferred."
  nullable    = false
  type = object({
    account_id                  = string
    region                      = string
    tenant_id                   = string
    product_id                  = string
    project_id                  = string
    configuration_id            = string
    source_scope_sha256         = string
    connection_role_arn         = string
    operator_role_arn           = string
    agent_space_id              = string
    agent_space_name            = string
    original_setup_operation_id = string
    original_intent_hash        = string
  })
  validation {
    condition = try(
      can(regex("^[0-9]{12}$", var.selection.account_id)) &&
      var.selection.region == "us-east-1" &&
      alltrue([for value in [var.selection.tenant_id, var.selection.product_id, var.selection.project_id, var.selection.configuration_id] :
      can(regex("^[A-Za-z0-9][A-Za-z0-9_.-]{2,127}$", value)) && !can(regex("(?i)(unresolved|placeholder|example)", value))]) &&
      can(regex("^[a-f0-9]{64}$", var.selection.source_scope_sha256)) &&
      can(regex("^[a-f0-9]{64}$", var.selection.original_intent_hash)) &&
      can(regex("^[a-f0-9-]{36}$", var.selection.original_setup_operation_id)) &&
      var.selection.agent_space_name == "adp-${var.selection.original_setup_operation_id}" &&
      can(regex("^as-[a-f0-9-]{36}$", var.selection.agent_space_id)) &&
      alltrue([for role in [var.selection.connection_role_arn, var.selection.operator_role_arn] :
      can(regex("^arn:aws:iam::${var.selection.account_id}:role/[A-Za-z0-9_+=,.@/-]+$", role))]), false
    )
    error_message = "Resolve every case/source/role field and the exact original ADP space/operation/intent. Only same-account explicit roles and us-east-1 are supported; wildcards/placeholders are refused."
  }
}

variable "source_retention_days" {
  description = "Source expiry; must cover the configured run and recovery window."
  type        = number
  default     = 30
  validation {
    condition     = var.source_retention_days >= 7 && var.source_retention_days <= 90 && floor(var.source_retention_days) == var.source_retention_days
    error_message = "Choose a reviewed integer source retention of 7–90 days."
  }
}

variable "log_retention_days" {
  description = "Operator follow-up policy for service-created log groups; this template does not automate it."
  type        = number
  default     = 30
  validation {
    condition     = contains([7, 14, 30, 60, 90], var.log_retention_days)
    error_message = "Choose a reviewed CloudWatch retention value."
  }
}
