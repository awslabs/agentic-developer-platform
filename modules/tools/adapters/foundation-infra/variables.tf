variable "aws_account_id" {
  type        = string
  description = "Confirmed account of the reviewed EKS target."
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "A confirmed 12-digit account is required."
  }
}

variable "aws_region" {
  type = string
  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9]+$", var.aws_region))
    error_message = "A concrete AWS region is required."
  }
}

variable "environment" {
  type        = string
  description = "Reviewed environment used to scope the isolated MCP Identity resources."
  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "Select a reviewed tools release environment."
  }
}

variable "cluster_name" {
  type        = string
  description = "Existing reviewed EKS cluster; the parent must bind its Kubernetes provider to this cluster."
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$", var.cluster_name))
    error_message = "Select the reviewed EKS cluster name."
  }
}

variable "namespace" {
  type        = string
  description = "Dedicated adapter namespace owned by this module, shared with the reviewed renderer input."
  validation {
    condition = (
      can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.namespace)) &&
      !contains(["default", "kube-system", "kube-public", "kube-node-lease"], var.namespace)
    )
    error_message = "Use a dedicated namespace, not a Kubernetes system namespace."
  }
}

variable "oidc_provider_arn" {
  type        = string
  description = "Existing IAM OIDC provider for this exact EKS cluster; its issuer and STS audience are verified."
  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:oidc-provider/oidc\\.eks\\.[a-z0-9-]+\\.amazonaws\\.com/id/[A-Za-z0-9]+$", var.oidc_provider_arn))
    error_message = "An EKS IAM OIDC provider ARN is required."
  }
}

variable "runtime_role_name" {
  type        = string
  description = "Dedicated adapter pod role. Foundation creates trust only; provider permissions are a later reviewed change."
  validation {
    condition     = can(regex("^[A-Za-z0-9_+=,.@-]{1,64}$", var.runtime_role_name))
    error_message = "Select a concrete dedicated IAM role name, not an ARN or path."
  }
}

variable "atlassian_mcp_identity_enabled" {
  type        = bool
  description = "Keep foundation free of user-token grants until observation-mode qualification explicitly enables them."
  default     = false
}

variable "runtime_verifier_group" {
  type        = string
  description = "Existing environment connection's EKS group; this module does not create or change EKS access entries."
  validation {
    condition     = can(regex("^adp:runtime-verifier-[a-z0-9][a-z0-9-]{0,62}$", var.runtime_verifier_group))
    error_message = "Select the environment's adp:runtime-verifier group."
  }
}
