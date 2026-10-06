variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Supply the selected 12-digit account ID."
  }
}

variable "region" {
  type = string
  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[1-9][0-9]*$", var.region))
    error_message = "Supply the selected AWS region."
  }
}

variable "environment" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,30}$", var.environment))
    error_message = "Supply the exact installation environment."
  }
}

variable "cluster_name" {
  type = string
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$", var.cluster_name))
    error_message = "Supply the selected EKS cluster name."
  }
}

variable "namespace" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,39}$", var.namespace))
    error_message = "Supply the existing management namespace."
  }
}

variable "oidc_issuer" {
  type = string
  validation {
    condition     = can(regex("^https://oidc\\.eks\\.[a-z0-9-]+\\.amazonaws\\.com/id/[A-Za-z0-9]+$", var.oidc_issuer))
    error_message = "Supply the observed exact EKS OIDC issuer."
  }
}

variable "installation_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9a-f]{24}$", var.installation_id))
    error_message = "Supply the existing installation identity."
  }
}

variable "api_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z0-9]{10}$", var.api_id))
    error_message = "Supply the selected Gateway API ID."
  }
}

variable "api_stage" {
  type = string
  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{1,128}$", var.api_stage))
    error_message = "Supply the selected Gateway stage."
  }
}

variable "keda_operator_role_arn" {
  type = string
  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_-]{1,64}$", var.keda_operator_role_arn))
    error_message = "Supply the existing exact KEDA operator role ARN."
  }
}

variable "operator_role_arn" {
  type = string
  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_-]{1,64}$", var.operator_role_arn))
    error_message = "Supply the selected same-account operator role ARN."
  }
}
