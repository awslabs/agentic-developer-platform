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
  default = "ON"
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

# =============================================================================
# Test Users Configuration (Issue #60)
# =============================================================================
# Gate test user provisioning behind a variable so prod deploys don't auto-
# create test accounts. Dev environments set this to true.

variable "create_test_users" {
  type        = bool
  description = "Create test users (admins group, test user, test admin) for dev/test environments. Never enable in production."
  default     = false
}

variable "test_user_email" {
  type        = string
  description = "Email for the non-admin test user"
  default     = "adp-test@example.com"
}

variable "test_admin_email" {
  type        = string
  description = "Email for the admin test user"
  default     = "adp-test-admin@example.com"
}

# =============================================================================
# GitHub OAuth Identity Provider (Issue #313)
# =============================================================================

variable "enable_github_oauth" {
  type        = bool
  description = "Enable GitHub as a federated identity provider via OAuth/OIDC"
  default     = false
}

variable "github_oauth_client_id" {
  type        = string
  description = "GitHub OAuth App client ID. Required when enable_github_oauth is true."
  default     = ""
  sensitive   = false
}

variable "github_oauth_client_secret" {
  type        = string
  description = "GitHub OAuth App client secret. Required when enable_github_oauth is true."
  default     = ""
  sensitive   = true
}

# =============================================================================
# Pre Sign-Up Configuration (Issue #314)
# =============================================================================

variable "pre_signup_allowlist_mode" {
  type        = string
  description = "Allowlist mode for Pre Sign-Up trigger: 'org' (GitHub org membership), 'explicit' (DDB allowlist), or 'open' (allow all)"
  default     = "org"
  validation {
    condition     = contains(["org", "explicit", "open"], var.pre_signup_allowlist_mode)
    error_message = "Allowlist mode must be 'org', 'explicit', or 'open'."
  }
}

variable "pre_signup_allowed_orgs" {
  type        = string
  description = "Comma-separated list of GitHub org names allowed to sign up (used when allowlist_mode is 'org')"
  default     = ""
}

variable "github_token_secret_arn" {
  type        = string
  description = "ARN of the Secrets Manager secret containing a GitHub API token for org membership checks. Required when allowlist_mode is 'org'."
  default     = ""
}

variable "kms_key_arn" {
  description = "KMS key ARN for DynamoDB server-side encryption"
  type        = string
}

variable "cloudwatch_kms_key_arn" {
  description = "ARN of the KMS key for CloudWatch Log Group encryption (CKV_AWS_158)"
  type        = string
  default     = ""
}

variable "enable_reserved_concurrency" {
  description = "Enable reserved concurrent executions on Cognito trigger Lambdas. Set to false on fresh accounts where Lambda quota is too low (Issue #2910)."
  type        = bool
  default     = true
}
