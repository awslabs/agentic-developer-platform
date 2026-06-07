variable "name_prefix" {
  description = "Resource name prefix"
  type        = string
}

variable "cluster_arn" {
  description = "ECS cluster ARN to run the dream cycle task in"
  type        = string
}

variable "task_def" {
  description = "ECS task definition ARN"
  type        = string
}

variable "subnet_ids" {
  description = "Subnet IDs for the scheduled task"
  type        = list(string)
}

variable "sg_id" {
  description = "Security group ID for the scheduled task"
  type        = string
}

variable "role_arn" {
  description = "IAM role ARN for EventBridge to invoke ECS"
  type        = string
}
