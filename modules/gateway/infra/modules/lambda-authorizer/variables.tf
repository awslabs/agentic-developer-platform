# =============================================================================
# Variables for Lambda Authorizer Module (Issue #239)
# =============================================================================

# =============================================================================
# Standard Module Variables
# =============================================================================

variable "environment" {
  description = "Environment name (e.g., dev, staging, prod)"
  type        = string
}

variable "name_prefix" {
  description = "Prefix for resource names (e.g., bedrockgw-dev)"
  type        = string
}

variable "common_tags" {
  description = "Common tags to apply to all resources"
  type        = map(string)
  default     = {}
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

# =============================================================================
# Cognito Configuration
# =============================================================================

variable "cognito_user_pool_id" {
  description = "Cognito User Pool ID for JWT validation"
  type        = string
}

# =============================================================================
# API Gateway Configuration
# =============================================================================

variable "api_gateway_id" {
  description = "ID of the API Gateway REST API to attach the authorizer to"
  type        = string
}

variable "api_gateway_execution_arn" {
  description = "Execution ARN of the API Gateway for Lambda permissions"
  type        = string
}

# =============================================================================
# S3 Artifact Configuration (Issue #408)
# =============================================================================

variable "lambda_artifact_bucket" {
  description = "S3 bucket containing the pre-built PyJWT layer zip (uploaded by CodeBuild)"
  type        = string
}

# =============================================================================
# Lambda Configuration
# =============================================================================

variable "lambda_memory_size" {
  description = "Lambda function memory size in MB"
  type        = number
  default     = 256
}

variable "lambda_timeout" {
  description = "Lambda function timeout in seconds"
  type        = number
  default     = 10
}

variable "authorizer_cache_ttl" {
  description = "Authorizer result cache TTL in seconds (0 to disable caching)"
  type        = number
  default     = 300

  validation {
    condition     = var.authorizer_cache_ttl >= 0 && var.authorizer_cache_ttl <= 3600
    error_message = "Authorizer cache TTL must be between 0 and 3600 seconds."
  }
}

# =============================================================================
# CloudWatch Logging
# =============================================================================

variable "log_retention_days" {
  description = "CloudWatch log retention in days"
  type        = number
  default     = 30

  validation {
    condition     = contains([0, 1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1827, 3653], var.log_retention_days)
    error_message = "Log retention days must be a valid CloudWatch retention value."
  }
}
