variable "environment" {
  type = string
  validation {
    condition     = contains(["dev", "test", "prod"], var.environment)
    error_message = "Select a protected deployment environment."
  }
}

variable "aws_account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "Select the account owning the existing connection role."
  }
}

variable "aws_region" {
  type = string
  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9]+$", var.aws_region))
    error_message = "Select one AWS Region."
  }
}

variable "connection_role_name" {
  type = string
  validation {
    condition     = can(regex("^[A-Za-z0-9+=,.@_-]{1,64}$", var.connection_role_name))
    error_message = "Select an existing Product connection role by name."
  }
}

variable "security" {
  description = "Enable only the native Security operations and registered source stores selected for this connection."
  type = object({
    enabled             = bool
    source_bucket_names = set(string)
    execution_role_arns = set(string)
  })
  default = { enabled = false, source_bucket_names = [], execution_role_arns = [] }
  validation {
    condition = alltrue([for bucket in var.security.source_bucket_names : can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", bucket))]) && alltrue([
      for role in var.security.execution_role_arns : can(regex("^arn:aws:iam::${var.aws_account_id}:role/[A-Za-z0-9+=,.@_/-]+$", role))
    ])
    error_message = "Security stores must be bucket names and execution roles must belong to the selected account."
  }
}

variable "finops" {
  description = "Enable regional native workspace access for Product-selected agents; FinOps IAM does not support per-agent resources. Billing views and role reads retain exact scope."
  type = object({
    agent_arns        = set(string)
    billing_view_arns = set(string)
    data_role_arns    = set(string)
  })
  default = { agent_arns = [], billing_view_arns = [], data_role_arns = [] }
  validation {
    condition = alltrue([
      for agent in var.finops.agent_arns : can(regex("^arn:aws:finops-agent:${var.aws_region}:${var.aws_account_id}:agentspace/[A-Za-z0-9_-]+$", agent))
      ]) && alltrue([
      for view in var.finops.billing_view_arns : can(regex("^arn:aws:billing::${var.aws_account_id}:billingview/[A-Za-z0-9_-]+$", view))
      ]) && alltrue([
      for role in var.finops.data_role_arns : can(regex("^arn:aws:iam::${var.aws_account_id}:role/[A-Za-z0-9+=,.@_/-]+$", role))
    ]) && (length(var.finops.agent_arns) == 0 || (length(var.finops.billing_view_arns) > 0 && length(var.finops.data_role_arns) > 0))
    error_message = "FinOps requires native agents, billing views and data roles in the selected account and region."
  }
}

variable "devops" {
  description = "Adopt selected existing native spaces, run finite investigations and open authenticated sessions."
  type = object({
    agent_space_arns           = set(string)
    knowledge_agent_space_arns = optional(set(string), [])
  })
  default = { agent_space_arns = [] }
  validation {
    condition = alltrue([
      for space in setunion(var.devops.agent_space_arns, var.devops.knowledge_agent_space_arns) : can(regex("^arn:aws:aidevops:${var.aws_region}:${var.aws_account_id}:agentspace/[A-Za-z0-9_-]+$", space))
    ])
    error_message = "DevOps spaces must belong to the selected account and region."
  }
}
