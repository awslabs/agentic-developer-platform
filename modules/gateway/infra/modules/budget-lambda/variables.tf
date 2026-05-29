# =============================================================================
# Variables for Budget Lambda Module (Issue #234)
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

# S3 Chat Logs Bucket
variable "chat_logs_bucket_name" {
  description = "Name of the S3 bucket containing chat logs"
  type        = string
}

variable "chat_logs_bucket_arn" {
  description = "ARN of the S3 bucket containing chat logs"
  type        = string
}

# VPC Configuration
variable "vpc_id" {
  description = "ID of the VPC for Lambda deployment"
  type        = string
}

variable "private_subnet_ids" {
  description = "List of private subnet IDs for Lambda deployment"
  type        = list(string)
}

# RDS Configuration
variable "rds_security_group_id" {
  description = "Security group ID for RDS access"
  type        = string
}

variable "db_host" {
  description = "RDS database hostname"
  type        = string
}

variable "db_port" {
  description = "RDS database port"
  type        = number
  default     = 5432
}

variable "db_name" {
  description = "Database name"
  type        = string
}

variable "db_username" {
  description = "Database username"
  type        = string
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "lambda_artifact_bucket" {
  description = "S3 bucket containing the pre-built psycopg2 layer zip (uploaded by CodeBuild)"
  type        = string
}

# Optional: RDS resource ID for IAM auth policy
variable "rds_resource_id" {
  description = "RDS instance resource ID (dbi-xxx format) for IAM auth. If empty, uses wildcard."
  type        = string
  default     = ""
}

# Lambda Configuration
variable "usage_tracker_memory" {
  description = "Memory allocation for usage tracker Lambda (MB)"
  type        = number
  default     = 256
}

variable "usage_tracker_timeout" {
  description = "Timeout for usage tracker Lambda (seconds)"
  type        = number
  default     = 30
}

variable "pricing_refresh_memory" {
  description = "Memory allocation for pricing refresh Lambda (MB)"
  type        = number
  default     = 256
}

variable "pricing_refresh_timeout" {
  description = "Timeout for pricing refresh Lambda (seconds)"
  type        = number
  default     = 60
}

variable "pricing_refresh_schedule" {
  description = "EventBridge schedule expression for pricing refresh"
  type        = string
  default     = "cron(0 6 * * ? *)" # Daily at 6 AM UTC
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days"
  type        = number
  default     = 30
}
