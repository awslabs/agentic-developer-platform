# =============================================================================
# Variables for S3 Chat Logs Module (Issue #143)
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

variable "kms_key_arn" {
  description = "KMS key ARN for SSE-KMS encryption. If empty, uses SSE-S3 (AES256)"
  type        = string
  default     = ""
}

variable "log_bucket_name" {
  description = "S3 bucket name for access logging. If empty, logging is disabled"
  type        = string
  default     = ""
}
