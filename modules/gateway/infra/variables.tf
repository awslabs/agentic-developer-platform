# =============================================================================
# Gateway Module Variables
# =============================================================================
# Only gateway-specific configuration. VPC, EKS, ECR, and base IAM are
# provided by the shared platform via terraform_remote_state.
# =============================================================================

# Environment Configuration
variable "environment" {
  type        = string
  description = "Environment name: dev, test, or prod"
  validation {
    condition     = contains(["dev", "test", "prod"], var.environment)
    error_message = "Environment must be dev, test, or prod."
  }
}

variable "aws_region" {
  type        = string
  description = "AWS region for resources"
  default     = "us-east-1"
}

variable "account_id" {
  type        = string
  description = "AWS account ID (used to construct the remote state bucket name)"
}

variable "cost_center" {
  type        = string
  description = "Cost center for billing allocation"
}

# =============================================================================
# RDS Configuration
# =============================================================================

variable "rds_instance_class" {
  type        = string
  description = "RDS instance class"
  default     = "db.t4g.medium"
}

variable "rds_allocated_storage" {
  type        = number
  description = "Initial allocated storage for RDS in GB"
  default     = 100
}

variable "rds_max_allocated_storage" {
  type        = number
  description = "Maximum allocated storage for RDS in GB"
  default     = 1000
}

variable "rds_multi_az" {
  type        = bool
  description = "Enable RDS Multi-AZ deployment"
  default     = false
}

variable "rds_backup_retention_period" {
  type        = number
  description = "RDS backup retention period in days"
  default     = 7
}

variable "rds_backup_window" {
  type        = string
  description = "RDS backup window"
  default     = "03:00-04:00"
}

variable "rds_maintenance_window" {
  type        = string
  description = "RDS maintenance window"
  default     = "sun:04:00-sun:05:00"
}

variable "rds_db_name" {
  type        = string
  description = "RDS database name"
  default     = "bedrockgateway"
}

variable "rds_username" {
  type        = string
  description = "RDS master username"
  default     = "bgadmin"
}

variable "enable_rds_iam_auth" {
  type        = bool
  description = "Enable RDS IAM authentication policy on the gateway IRSA role"
  default     = true
}

# =============================================================================
# Redis Configuration
# =============================================================================

variable "enable_redis" {
  type        = bool
  description = "Enable ElastiCache Redis cluster"
  default     = true
}

variable "redis_node_type" {
  type        = string
  description = "ElastiCache Redis node type"
  default     = "cache.t3.micro"
}

variable "redis_num_cache_nodes" {
  type        = number
  description = "Number of cache nodes for Redis cluster"
  default     = 1
}

variable "redis_parameter_group_name" {
  type        = string
  description = "Redis parameter group name"
  default     = "default.redis7"
}

variable "redis_port" {
  type        = number
  description = "Redis port"
  default     = 6379
}

variable "enable_elasticache_iam_auth" {
  type        = bool
  description = "Enable ElastiCache IAM authentication policy on the gateway IRSA role"
  default     = true
}

# =============================================================================
# IAM Configuration
# =============================================================================

variable "pool_account_arns" {
  type        = list(string)
  description = "List of AWS account ARNs that contain Bedrock pools for cross-account access"
  default     = []
}

# =============================================================================
# Cognito Configuration
# =============================================================================

variable "cognito_mfa_configuration" {
  type        = string
  description = "MFA configuration for Cognito User Pool: OFF, ON, or OPTIONAL"
  default     = "OPTIONAL"
  validation {
    condition     = contains(["OFF", "ON", "OPTIONAL"], var.cognito_mfa_configuration)
    error_message = "MFA configuration must be OFF, ON, or OPTIONAL."
  }
}

variable "cognito_callback_urls" {
  type        = list(string)
  description = "List of allowed callback URLs for the Cognito User Pool client (OAuth 2.0 redirect URIs)"
  default     = ["http://localhost:5173/auth/callback"]
}

variable "cognito_logout_urls" {
  type        = list(string)
  description = "List of allowed logout URLs for the Cognito User Pool client"
  default     = ["http://localhost:5173"]
}

variable "cognito_custom_domain" {
  type        = string
  description = "Custom domain for the Cognito User Pool (optional). If not provided, uses Cognito hosted domain."
  default     = ""
}

variable "cognito_access_token_validity" {
  type        = number
  description = "Access token validity in minutes (default: 60 = 1 hour)"
  default     = 60
}

variable "cognito_refresh_token_validity" {
  type        = number
  description = "Refresh token validity in minutes (default: 43200 = 30 days)"
  default     = 43200
}

variable "cognito_id_token_validity" {
  type        = number
  description = "ID token validity in minutes (default: 60 = 1 hour)"
  default     = 60
}

# =============================================================================
# GitHub OAuth Identity Provider (Issue #313)
# =============================================================================

variable "enable_github_oauth" {
  type        = bool
  description = "Enable GitHub as a federated identity provider in Cognito via OAuth/OIDC"
  default     = false
}

variable "github_oauth_client_id" {
  type        = string
  description = "GitHub OAuth App client ID. Required when enable_github_oauth is true."
  default     = ""
}

variable "github_oauth_client_secret" {
  type        = string
  description = "GitHub OAuth App client secret. Required when enable_github_oauth is true."
  default     = ""
  sensitive   = true
}

# =============================================================================
# GitHub Auth Broker Configuration (Issue #520)
# =============================================================================

variable "github_auth_allowlist_mode" {
  type        = string
  description = "Allowlist mode for GitHub auth broker: open, org, or explicit"
  default     = "open"
}

variable "github_auth_allowed_orgs" {
  type        = string
  description = "Comma-separated GitHub orgs for allowlist_mode=org"
  default     = ""
}

variable "github_auth_token_secret_arn" {
  type        = string
  description = "Secrets Manager ARN for GitHub API token used in org membership checks"
  default     = ""
}

# =============================================================================
# Frontend Configuration
# =============================================================================

variable "frontend_domain_name" {
  type        = string
  description = "Custom domain name for CloudFront frontend (optional)"
  default     = ""
}

variable "frontend_acm_certificate_arn" {
  type        = string
  description = "ACM certificate ARN for custom domain (required if frontend_domain_name is set)"
  default     = ""
}

variable "enable_frontend_waf" {
  type        = bool
  description = "Enable WAF web ACL for CloudFront frontend"
  default     = false
}

# =============================================================================
# VPC Origin Configuration (for internal ALB)
# =============================================================================

variable "enable_vpc_origin" {
  type        = bool
  description = "Enable CloudFront VPC Origin for internal ALB access. When true, the ALB should be configured as internal (scheme: internal)."
  default     = false
}

variable "internal_alb_arn" {
  type        = string
  description = "ARN of the internal ALB for CloudFront VPC Origin and API Gateway VPC Link v2. For EKS Ingress-managed ALBs, this is set dynamically in the backend-deploy workflow."
  default     = ""
}

variable "internal_alb_dns" {
  description = "DNS name of the internal ALB. Set dynamically by the deploy workflow after the EKS Ingress ALB is created. (Issue #42)"
  type        = string
  default     = "localhost"
}

variable "alb_security_group_ids" {
  description = "Security group IDs of the internal ALB. Used by API Gateway VPC Link v2 SG for egress rules. Set dynamically by the deploy workflow. (Issue #42)"
  type        = list(string)
  default     = []
}

variable "vpc_origin_read_timeout" {
  type        = number
  description = "Origin read timeout in seconds for API requests (applies to both custom origin and VPC origin). Set to 180 for SSE streaming support."
  default     = 180
}

variable "vpc_origin_keepalive_timeout" {
  type        = number
  description = "Origin keepalive timeout in seconds for API requests."
  default     = 60
}

# =============================================================================
# Chat Logging Configuration (Issue #143)
# =============================================================================

variable "enable_chat_logging" {
  type        = bool
  description = "Enable async chat logging to S3 with PII scrubbing"
  default     = false
}

variable "chat_logging_scrub_level" {
  type        = string
  description = "Chat logging scrub level: off, basic (headers+regex), or standard (headers+regex+Comprehend PII)"
  default     = "standard"
  validation {
    condition     = contains(["off", "basic", "standard"], var.chat_logging_scrub_level)
    error_message = "Chat logging scrub level must be off, basic, or standard."
  }
}

variable "chat_logging_kms_key_arn" {
  type        = string
  description = "KMS key ARN for S3 SSE-KMS encryption of chat logs. If empty, uses SSE-S3 (AES256)."
  default     = ""
}

# =============================================================================
# Distributed Tracing Configuration (Issue #144)
# =============================================================================

variable "enable_xray_tracing" {
  type        = bool
  description = "Enable X-Ray tracing IAM permissions for the gateway service role."
  default     = false
}

# =============================================================================
# CloudFront Access Logging
# =============================================================================

variable "enable_cloudfront_logging" {
  type        = bool
  description = "Enable CloudFront standard access logging to S3."
  default     = false
}

variable "cloudfront_log_retention_days" {
  type        = number
  description = "Number of days to retain CloudFront access logs in S3"
  default     = 90
}

# =============================================================================
# CloudWatch Latency Dashboard (Issue #144)
# =============================================================================

variable "alb_arn_suffix" {
  type        = string
  description = "ALB ARN suffix for CloudWatch metrics (format: app/<name>/<id>). Set after the EKS Ingress ALB is created. Leave empty if ALB not yet provisioned."
  default     = ""
}

# =============================================================================
# API Gateway Configuration (Issue #236)
# =============================================================================

variable "enable_api_gateway" {
  type        = bool
  description = "Enable API Gateway REST API as alternate route to ALB (with streaming support)"
  default     = false
}

variable "api_gateway_throttle_burst_limit" {
  type        = number
  description = "API Gateway throttling burst limit (requests per second)"
  default     = 100
}

variable "api_gateway_throttle_rate_limit" {
  type        = number
  description = "API Gateway throttling rate limit (requests per second)"
  default     = 50
}

variable "api_gateway_log_retention_days" {
  type        = number
  description = "CloudWatch log retention in days for API Gateway access logs"
  default     = 30
}

# =============================================================================
# Test Users Configuration (Issue #60)
# =============================================================================

variable "create_test_users" {
  type        = bool
  description = "Create Cognito test users (admins group, test user, test admin) with Secrets Manager credentials. For dev/test only — never enable in production."
  default     = false
}
