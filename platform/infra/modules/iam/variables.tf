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

variable "cluster_name" {
  type        = string
  description = "Name of the EKS cluster for OIDC configuration"
}

variable "pool_account_arns" {
  type        = list(string)
  description = "List of AWS account ARNs that contain Bedrock pools for cross-account access"
  default     = []
}

variable "create_instance_profile" {
  type        = bool
  description = "Whether to create an IAM instance profile for EKS nodes (set to false for testing if lacking iam:TagInstanceProfile permission)"
  default     = true
}

# RDS IAM Authentication variables
variable "enable_rds_iam_auth" {
  type        = bool
  description = "Enable RDS IAM database authentication policy for the gateway service role"
  default     = true
}

variable "rds_db_username" {
  type        = string
  description = "The database username for IAM authentication"
  default     = "bgadmin"
}

# ElastiCache IAM Authentication variables
variable "enable_elasticache_iam_auth" {
  type        = bool
  description = "Enable ElastiCache IAM authentication policy for the gateway service role"
  default     = true
}

variable "redis_replication_group_id" {
  type        = string
  description = "The ID of the Redis replication group for IAM auth"
  default     = ""
}

variable "redis_iam_user_id" {
  type        = string
  description = "The ID of the Redis IAM user for IAM auth"
  default     = ""
}

# Chat Logging variables (Issue #143)
variable "enable_chat_logging" {
  type        = bool
  description = "Enable chat logging permissions for the gateway service role"
  default     = false
}

variable "chat_logs_bucket_arn" {
  type        = string
  description = "ARN of the S3 bucket for chat logs"
  default     = ""
}

variable "enable_comprehend_pii" {
  type        = bool
  description = "Enable Comprehend PII detection permissions (for standard scrubbing level)"
  default     = true
}

# Issue #144: X-Ray Tracing variables
variable "enable_xray_tracing" {
  type        = bool
  description = "Enable X-Ray tracing permissions for the gateway service role"
  default     = false
}
