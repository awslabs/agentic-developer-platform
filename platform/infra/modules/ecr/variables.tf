variable "environment" {
  type        = string
  description = "Environment name (dev, test, prod)"
}

variable "name_prefix" {
  type        = string
  description = "Name prefix used by the registry scanning rule filter"
}

variable "repositories" {
  type        = list(string)
  description = "Names of ECR repositories to create"
}

variable "common_tags" {
  type        = map(string)
  description = "Common tags to apply to all resources"
  default     = {}
}

variable "image_tag_mutability" {
  type        = string
  description = "ECR image tag mutability"
  default     = "MUTABLE"
  validation {
    condition     = contains(["MUTABLE", "IMMUTABLE"], var.image_tag_mutability)
    error_message = "ECR image tag mutability must be MUTABLE or IMMUTABLE."
  }
}

variable "scan_on_push" {
  type        = bool
  description = "Enable ECR image scanning on push"
  default     = true
}

variable "lifecycle_policy_rules" {
  type        = number
  description = "Number of images to retain in ECR repository for production tags"
  default     = 10
}

variable "cross_account_arns" {
  type        = list(string)
  description = "List of cross-account ARNs that can access this ECR repository"
  default     = []
}

variable "enable_pull_through_cache" {
  type        = bool
  description = "Enable ECR pull through cache for upstream registries (requires ecr:CreatePullThroughCacheRule permission)"
  default     = false
}

variable "dockerhub_credentials_arn" {
  type        = string
  description = "ARN of Secrets Manager secret containing Docker Hub credentials"
  default     = ""
}

variable "enable_event_notifications" {
  type        = bool
  description = "Enable EventBridge notifications for ECR events"
  default     = false
}

variable "sns_topic_arn" {
  type        = string
  description = "SNS topic ARN for ECR event notifications"
  default     = ""
}