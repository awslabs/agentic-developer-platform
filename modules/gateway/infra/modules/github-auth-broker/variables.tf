# =============================================================================
# GitHub Auth Broker Lambda — Variables
# Issue #520
# =============================================================================

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
}

variable "name_prefix" {
  description = "Prefix for resource naming"
  type        = string
}

variable "common_tags" {
  description = "Common tags for all resources"
  type        = map(string)
  default     = {}
}

variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "cognito_user_pool_id" {
  description = "Cognito User Pool ID"
  type        = string
}

variable "cognito_user_pool_arn" {
  description = "Cognito User Pool ARN"
  type        = string
}

variable "cognito_client_id" {
  description = "Cognito App Client ID (must have ADMIN_USER_PASSWORD_AUTH enabled)"
  type        = string
}

variable "github_oauth_secret_arn" {
  description = "Secrets Manager ARN for GitHub OAuth credentials"
  type        = string
}

variable "frontend_url" {
  description = "Frontend origin URL (e.g., https://d1g6cal2ts4iis.cloudfront.net)"
  type        = string
}

variable "allowlist_mode" {
  description = "Allowlist mode: 'org' (GitHub org membership), 'explicit' (not implemented; denies), or 'open' (no enforcement — requires allow_open_signup). Issue #3986: defaults to 'org' so the module fails closed."
  type        = string
  default     = "org"
  validation {
    condition     = contains(["org", "explicit", "open"], var.allowlist_mode)
    error_message = "Allowlist mode must be 'org', 'explicit', or 'open'."
  }
  validation {
    condition     = var.allowlist_mode != "org" || trimspace(var.allowed_orgs) != ""
    error_message = "allowed_orgs must be set when allowlist_mode is 'org' (an empty org list denies every sign-in)."
  }
  validation {
    condition     = var.allowlist_mode != "open" || var.allow_open_signup
    error_message = "allowlist_mode = 'open' disables allowlist enforcement entirely; set allow_open_signup = true to acknowledge this."
  }
}

variable "allowed_orgs" {
  description = "Comma-separated list of allowed GitHub orgs (required for mode=org)"
  type        = string
  default     = ""
}

variable "allow_open_signup" {
  description = "Escape hatch (#3986): honour allowlist_mode = 'open', which lets ANY GitHub user provision a Cognito account."
  type        = bool
  default     = false
}

variable "github_token_secret_arn" {
  description = "Secrets Manager ARN for GitHub API token (org membership checks). Without it the broker falls back to the signing-in user's own OAuth token, which cannot verify membership unless the OAuth App is org-approved."
  type        = string
  default     = ""
}

variable "lambda_artifact_bucket" {
  description = "S3 bucket for Lambda deployment artifacts"
  type        = string
}

variable "cloudwatch_kms_key_arn" {
  description = "ARN of the KMS key for CloudWatch Log Group encryption (CKV_AWS_158)"
  type        = string
  default     = ""
}

variable "enable_reserved_concurrency" {
  description = "Enable reserved concurrent executions. Set to false on fresh accounts where Lambda quota is too low (Issue #2910)."
  type        = bool
  default     = true
}

# --- API Gateway variables (Issue #525, removed in Issue #1011) ---
# The route is now defined in the api-gateway module's OpenAPI body.
# The broker module only needs to expose its invoke_arn as an output.
