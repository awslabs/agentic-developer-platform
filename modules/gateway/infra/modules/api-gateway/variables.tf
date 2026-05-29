# =============================================================================
# Variables for API Gateway Module (Issue #236)
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
# VPC Configuration
# =============================================================================

variable "vpc_id" {
  description = "ID of the VPC containing the internal ALB"
  type        = string
}

variable "private_subnet_ids" {
  description = "List of private subnet IDs for the VPC Link"
  type        = list(string)
}

# =============================================================================
# ALB Configuration
# =============================================================================
# The ALB ARN and DNS are typically set dynamically by the backend-deploy
# workflow because the ALB is created by the EKS Ingress controller.

variable "internal_alb_arn" {
  description = "ARN of the internal ALB. Set dynamically by the deploy workflow after the EKS Ingress ALB is created."
  type        = string
  default     = ""
}

variable "internal_alb_dns" {
  description = "DNS name of the internal ALB. Set dynamically by the deploy workflow."
  type        = string
  default     = "localhost"
}

variable "alb_security_group_ids" {
  description = "Security group IDs of the internal ALB. The VPC Link v2 SG gets egress to these, and these get an ingress rule from the VPC Link SG. (Issue #42)"
  type        = list(string)
  default     = []
}

# =============================================================================
# Authentication Configuration (Optional)
# =============================================================================
# Start with NONE authorization since the backend already validates JWT.
# Cognito authorizer can be added later if needed.

variable "cognito_user_pool_arn" {
  description = "ARN of the Cognito User Pool for authorization (optional). If empty, authorization is NONE (backend handles JWT validation)."
  type        = string
  default     = ""
}

# =============================================================================
# Integration Configuration
# =============================================================================

variable "integration_timeout_ms" {
  description = "Integration timeout in milliseconds. Set to 900000 (15 minutes) for long-running LLM requests."
  type        = number
  default     = 900000 # 15 minutes

  validation {
    condition     = var.integration_timeout_ms >= 50 && var.integration_timeout_ms <= 900000
    error_message = "Integration timeout must be between 50ms and 900000ms (15 minutes)."
  }
}

# =============================================================================
# Throttling Configuration
# =============================================================================

variable "throttle_burst_limit" {
  description = "API Gateway throttling burst limit (requests per second)"
  type        = number
  default     = 100

  validation {
    condition     = var.throttle_burst_limit >= 0 && var.throttle_burst_limit <= 10000
    error_message = "Throttle burst limit must be between 0 and 10000."
  }
}

variable "throttle_rate_limit" {
  description = "API Gateway throttling rate limit (requests per second)"
  type        = number
  default     = 50

  validation {
    condition     = var.throttle_rate_limit >= 0 && var.throttle_rate_limit <= 10000
    error_message = "Throttle rate limit must be between 0 and 10000."
  }
}

# =============================================================================
# Logging Configuration
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

variable "enable_xray_tracing" {
  description = "Enable X-Ray tracing on the API Gateway stage"
  type        = bool
  default     = true
}

# =============================================================================
# GitHub Auth Broker Integration (Issue #1011)
# =============================================================================

variable "broker_lambda_invoke_arn" {
  description = "Invoke ARN of the GitHub auth broker Lambda. When set, adds /auth/github/{proxy+} route to the OpenAPI body."
  type        = string
  default     = ""
}

variable "broker_lambda_function_name" {
  description = "Function name of the GitHub auth broker Lambda (for aws_lambda_permission)."
  type        = string
  default     = ""
}
