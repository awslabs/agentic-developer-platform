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
  description = "List of private subnet IDs for DB subnet group"
}

variable "rds_security_group_id" {
  type        = string
  description = "Security group ID for RDS"
}

variable "instance_class" {
  type        = string
  description = "RDS instance class"
  default     = "db.t4g.medium"
}

variable "allocated_storage" {
  type        = number
  description = "Initial allocated storage for RDS in GB"
  default     = 100
}

variable "max_allocated_storage" {
  type        = number
  description = "Maximum allocated storage for RDS in GB"
  default     = 1000
}

variable "multi_az" {
  type        = bool
  description = "Enable RDS Multi-AZ deployment"
  default     = false
}

variable "backup_retention_period" {
  type        = number
  description = "RDS backup retention period in days"
  default     = 7
}

variable "backup_window" {
  type        = string
  description = "RDS backup window (UTC)"
  default     = "03:00-04:00"
}

variable "maintenance_window" {
  type        = string
  description = "RDS maintenance window (UTC)"
  default     = "sun:04:00-sun:05:00"
}

variable "db_name" {
  type        = string
  description = "Name of the database to create"
  default     = "bedrockgateway"
}

variable "username" {
  type        = string
  description = "Username for the master DB user"
  default     = "bgadmin"
}

variable "create_read_replica" {
  type        = bool
  description = "Create a read replica for the RDS instance (prod only)"
  default     = false
}

variable "replica_instance_class" {
  type        = string
  description = "Instance class for read replica (if different from main)"
  default     = ""
}

variable "cloudwatch_kms_key_arn" {
  description = "ARN of the KMS key for CloudWatch Log Group encryption (CKV_AWS_158)"
  type        = string
  default     = ""
}