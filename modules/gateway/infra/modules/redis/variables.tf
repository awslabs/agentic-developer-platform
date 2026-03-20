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
  description = "List of private subnet IDs for ElastiCache subnet group"
}

variable "redis_security_group_id" {
  type        = string
  description = "Security group ID for Redis"
}

variable "node_type" {
  type        = string
  description = "ElastiCache Redis node type"
  default     = "cache.t3.micro"
}

variable "num_cache_nodes" {
  type        = number
  description = "Number of cache nodes for Redis cluster"
  default     = 1
  validation {
    condition     = var.num_cache_nodes >= 1 && var.num_cache_nodes <= 6
    error_message = "Number of cache nodes must be between 1 and 6."
  }
}

variable "parameter_group_name" {
  type        = string
  description = "Redis parameter group name"
  default     = "default.redis7"
}

variable "port" {
  type        = number
  description = "Redis port"
  default     = 6379
}

variable "enable_monitoring" {
  type        = bool
  description = "Enable CloudWatch monitoring and alarms"
  default     = true
}

variable "sns_topic_arn" {
  type        = string
  description = "SNS topic ARN for alarm notifications"
  default     = ""
}