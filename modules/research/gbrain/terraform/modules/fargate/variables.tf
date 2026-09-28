variable "name_prefix" {
  description = "Resource name prefix"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID"
  type        = string
}

variable "subnet_ids" {
  description = "Private subnet IDs for ECS tasks"
  type        = list(string)
}

variable "service_sg_id" {
  description = "Security group ID for the ECS service"
  type        = string
}

variable "cpu" {
  description = "Task CPU units"
  type        = number
  default     = 1024
}

variable "memory" {
  description = "Task memory in MB"
  type        = number
  default     = 2048
}

variable "desired_count" {
  description = "Number of tasks to run"
  type        = number
  default     = 1
}

variable "container_image" {
  description = "Container image URI"
  type        = string
}

variable "db_endpoint" {
  description = "RDS endpoint (host:port)"
  type        = string
}

variable "db_credentials_arn" {
  description = "Secrets Manager ARN for DB credentials"
  type        = string
}

variable "mcp_token_arn" {
  description = "Secrets Manager ARN for MCP bearer token"
  type        = string
}

variable "s3_bucket_name" {
  description = "S3 bucket name for brain repository"
  type        = string
}

variable "log_group_name" {
  description = "CloudWatch log group name"
  type        = string
}

variable "task_role_arn" {
  description = "IAM role ARN for the application (task role)"
  type        = string
}

variable "execution_role_arn" {
  description = "IAM role ARN for ECS task execution"
  type        = string
}
