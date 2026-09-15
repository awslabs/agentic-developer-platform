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

variable "certificate_arn" {
  type        = string
  description = <<-EOT
    ACM certificate ARN for a Cognito custom domain. Required by AWS whenever
    custom_domain is a fully-qualified domain name; must live in us-east-1
    regardless of the pool's region. Leave empty (the default) to keep using a
    Cognito-prefix domain.

    NOTE: switching an existing pool from a prefix domain to a custom domain
    replaces aws_cognito_user_pool_domain, which briefly interrupts hosted-UI
    authentication. AWS also requires the parent domain to have a resolvable
    A record before it will accept the custom domain.
  EOT
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

variable "cli_refresh_token_validity" {
  type        = number
  description = "Refresh token validity in minutes for the CLI app client (default 1440 = 24 hours; deliberately much shorter than the SPA client's)"
  default     = 1440
  validation {
    condition     = var.cli_refresh_token_validity >= 60 && var.cli_refresh_token_validity <= 43200
    error_message = "CLI refresh token validity must be between 60 minutes (1 hour) and 43200 minutes (30 days)."
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
  description = "Allowlist mode for Pre Sign-Up trigger: 'org' (GitHub org membership), 'platform' (≥1 platform org membership — #4844), 'explicit' (DDB allowlist), or 'open' (allow all — requires pre_signup_allow_open_signup)"
  default     = "org"
  validation {
    condition     = contains(["org", "platform", "explicit", "open"], var.pre_signup_allowlist_mode)
    error_message = "Allowlist mode must be 'org', 'platform', 'explicit', or 'open'."
  }
  # Issue #4844: 'open' now requires the same acknowledgement flag the broker has
  # required since #3986. Caught in the plan rather than at runtime, where it
  # would surface as a total sign-up denial.
  validation {
    condition     = var.pre_signup_allowlist_mode != "open" || var.pre_signup_allow_open_signup
    error_message = "pre_signup_allowlist_mode = 'open' disables allowlist enforcement entirely; set pre_signup_allow_open_signup = true to acknowledge this."
  }
}

variable "pre_signup_allowed_orgs" {
  type        = string
  description = "Comma-separated list of GitHub org names allowed to sign up (used when allowlist_mode is 'org')"
  default     = ""
}

variable "pre_signup_allow_open_signup" {
  type        = bool
  description = "Escape hatch (#3986/#4844): honour pre_signup_allowlist_mode = 'open'. Mirrors the broker's allow_open_signup so both copies of the allowlist agree."
  default     = false
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

# -----------------------------------------------------------------------------
# Membership-eligibility projection read (Issue #4849)
# -----------------------------------------------------------------------------
# The pre-signup trigger reads `member_org_ids` off the identity-index rows to
# answer "does this GitHub identity hold any platform org membership?" without a
# gateway call. Names/ARNs arrive as variables rather than cross-module
# references: the tables live in the gateway root module, and referencing back
# into the root from here would close the cloudfront -> api_gateway ->
# github_auth_broker -> cloudfront dependency loop documented at
# modules/gateway/infra/main.tf:723-736.
#
# All default to empty/false so the read is inert until wired: an unset table name
# makes check_platform_membership return UNAVAILABLE, which in shadow mode is a
# log line and nothing more.

variable "identity_index_table_name" {
  description = "Name of the legacy identity-index DynamoDB table (Issue #4849 eligibility read)"
  type        = string
  default     = ""
}

variable "user_identity_index_table_name" {
  description = "Name of the v2 user-identity-index DynamoDB table (Issue #4849 eligibility read)"
  type        = string
  default     = ""
}

variable "identity_index_table_arns" {
  description = "ARNs of the identity-index tables the pre-signup Lambda may GetItem from. Empty grants nothing."
  type        = list(string)
  default     = []
}

variable "user_identity_index_v2_read" {
  description = "Read the v2 user-identity-index table first, falling back to the legacy table. Mirrors the webhook-ingress reader's flag (#537) so all readers move together."
  type        = string
  default     = "false"
}
