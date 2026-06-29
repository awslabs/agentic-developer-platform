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
  description = "Allowlist mode: open, org, or explicit"
  type        = string
  default     = "open"
}

variable "allowed_orgs" {
  description = "Comma-separated list of allowed GitHub orgs (for mode=org)"
  type        = string
  default     = ""
}

variable "github_token_secret_arn" {
  description = "Secrets Manager ARN for GitHub API token (org membership checks)"
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

# --- API Gateway variables (Issue #525, removed in Issue #1011) ---
# The route is now defined in the api-gateway module's OpenAPI body.
# The broker module only needs to expose its invoke_arn as an output.
