variable "environment" {
  type = string
}

variable "name_prefix" {
  type = string
}

variable "runner_role_name" {
  description = "Explicit role name when preserving an installation or avoiding an existing unrelated role"
  type        = string
  default     = ""
}

variable "oidc_provider_arn" {
  description = "ARN of the shared EKS OIDC provider"
  type        = string
}

variable "oidc_issuer" {
  description = "OIDC issuer URL of the shared EKS cluster"
  type        = string
}

variable "aws_region" {
  type = string
}

variable "runner_namespace" {
  description = "Kubernetes namespace for runner pods"
  type        = string
  default     = "arc-runners"
}

# A18 (#5674): see the trust policy in main.tf. Empty means "trust exactly
# var.runner_namespace", which is the behaviour every current caller wants —
# so no caller has to set this, and the wildcard is gone regardless.
variable "runner_trusted_namespaces" {
  description = "Additional namespaces whose github-runner-sa may assume the runner role, beyond var.runner_namespace. Exact matches — no patterns. Each entry is a reviewed grant of this role's deploy permissions to another namespace."
  type        = list(string)
  default     = []

  validation {
    # StringEquals does not glob, so a pattern here would silently match nothing
    # and invite someone to switch the operator back to StringLike.
    condition = alltrue([
      for namespace in var.runner_trusted_namespaces :
      !can(regex("[*?]", namespace))
    ])
    error_message = "runner_trusted_namespaces entries must be exact namespace names, not patterns (A18, #5674). List each namespace individually."
  }
}

variable "security_scans_bucket_arn" {
  description = "ARN of the security scans S3 bucket for SARIF archival"
  type        = string
  default     = ""
}

variable "transport_secret_arns" {
  description = "Exact legacy transport secret ARNs preserved during the separately authorized engine rollout. Validated by runner-runtime-policy."
  type        = list(string)
  default     = []
}

variable "gateway_execution_arns" {
  type        = list(string)
  default     = []
  description = "Reviewed exact API Gateway execution ARNs for runner transport; no API, stage or method wildcards."
}
