variable "environment" {
  type        = string
  description = "Environment name (dev, test, prod)"
}

variable "name_prefix" {
  type        = string
  description = "Name prefix for resources"
}

variable "common_tags" {
  type        = map(string)
  description = "Common tags to apply to all resources"
  default     = {}
}

variable "mfa_configuration" {
  type        = string
  description = "MFA configuration: OFF, ON, or OPTIONAL"
  # Issue #133: Changed default from OPTIONAL to ON for security
  # MFA is required for a SaaS platform managing Bedrock access
  default     = "ON"
  validation {
    condition     = contains(["OFF", "ON", "OPTIONAL"], var.mfa_configuration)
    error_message = "MFA configuration must be OFF, ON, or OPTIONAL."
  }
}

variable "callback_urls" {
  type        = list(string)
  description = "List of allowed callback URLs for the user pool client (OAuth 2.0 redirect URIs)"
  default     = ["http://localhost:5173/auth/callback"]
}

variable "logout_urls" {
  type        = list(string)
  description = "List of allowed logout URLs for the user pool client"
  default     = ["http://localhost:5173"]
}

variable "custom_domain" {
  type        = string
  description = "Custom domain for the user pool (optional). If not provided, uses Cognito hosted domain."
  default     = ""
}

variable "access_token_validity" {
  type        = number
  description = "Access token validity in minutes"
  default     = 60
  validation {
    condition     = var.access_token_validity >= 5 && var.access_token_validity <= 1440
    error_message = "Access token validity must be between 5 and 1440 minutes (24 hours)."
  }
}

variable "refresh_token_validity" {
  type        = number
  description = "Refresh token validity in minutes"
  default     = 43200 # 30 days
  validation {
    condition     = var.refresh_token_validity >= 60 && var.refresh_token_validity <= 525600
    error_message = "Refresh token validity must be between 60 minutes (1 hour) and 525600 minutes (365 days)."
  }
}

variable "id_token_validity" {
  type        = number
  description = "ID token validity in minutes"
  default     = 60
  validation {
    condition     = var.id_token_validity >= 5 && var.id_token_validity <= 1440
    error_message = "ID token validity must be between 5 and 1440 minutes (24 hours)."
  }
}

variable "password_minimum_length" {
  type        = number
  description = "Minimum password length"
  default     = 12
}

variable "enable_software_mfa" {
  type        = bool
  description = "Enable software token MFA (TOTP)"
  default     = true
}
