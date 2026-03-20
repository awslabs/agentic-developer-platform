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

variable "vpc_id" {
  type        = string
  description = "ID of the VPC"
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "List of private subnet IDs"
}

variable "eks_security_group_id" {
  type        = string
  description = "Security group ID for EKS cluster"
}

variable "cluster_version" {
  type        = string
  description = "Kubernetes version for EKS cluster"
  default     = "1.35"
}

variable "node_group_instance_types" {
  type        = list(string)
  description = "Instance types for EKS node group"
  default     = ["t3.medium"]
}

variable "node_group_desired_size" {
  type        = number
  description = "Desired number of nodes in EKS node group"
  default     = 2
}

variable "node_group_max_size" {
  type        = number
  description = "Maximum number of nodes in EKS node group"
  default     = 5
}

variable "node_group_min_size" {
  type        = number
  description = "Minimum number of nodes in EKS node group"
  default     = 1
}

variable "gateway_service_role_arn" {
  type        = string
  description = "ARN of the gateway service IAM role"
}

variable "node_group_role_arn" {
  type        = string
  description = "ARN of the EKS node group IAM role"
  default     = ""
}

variable "eks_public_access_cidrs" {
  type        = list(string)
  description = "CIDR blocks allowed to access EKS public endpoint"
  # No default — must be explicitly set per environment to prevent accidental 0.0.0.0/0 exposure
}

variable "ci_runner_role_arn" {
  type        = string
  description = "ARN of the CI runner IAM role for EKS API access"
  default     = ""
}

# IRSA Gateway Service Configuration
variable "enable_rds_iam_auth" {
  type        = bool
  description = "Enable RDS IAM authentication policy on the gateway IRSA role"
  default     = false
}

variable "rds_db_username" {
  type        = string
  description = "The database username for RDS IAM authentication"
  default     = "bgadmin"
}

variable "enable_elasticache_iam_auth" {
  type        = bool
  description = "Enable ElastiCache IAM authentication policy on the gateway IRSA role"
  default     = false
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

variable "pool_account_arns" {
  type        = list(string)
  description = "List of AWS account ARNs for cross-account Bedrock pool access"
  default     = []
}

# Issue #226: Cognito User Pool ID for Cognito-backed entity list endpoints
variable "cognito_user_pool_id" {
  type        = string
  description = "The Cognito User Pool ID for Cognito read permissions (e.g., us-east-1_AbCdEfGhI). If empty, Cognito IAM policy is not created."
  default     = ""
}

# Issue #143: Chat Logging — S3 and Comprehend permissions for IRSA role
variable "chat_logs_bucket_arn" {
  type        = string
  description = "ARN of the S3 bucket for chat logs. If empty, S3 chat logging policy is not created."
  default     = ""
}

variable "enable_comprehend_pii" {
  type        = bool
  description = "Enable Comprehend PII detection permissions on the gateway IRSA role"
  default     = false
}


# Container Insights — CloudWatch Observability addon
variable "enable_container_insights" {
  type        = bool
  description = "Enable CloudWatch Container Insights via the amazon-cloudwatch-observability EKS addon. Ships pod logs and metrics to CloudWatch."
  default     = false
}
