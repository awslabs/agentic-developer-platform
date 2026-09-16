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

variable "repository_encryption" {
  description = "Per-repository encryption overrides for retaining immutable settings of existing repositories"
  type = map(object({
    encryption_type = string
    kms_key         = optional(string)
  }))
  default = {}

  validation {
    condition = alltrue([
      for config in values(var.repository_encryption) :
      contains(["AES256", "KMS", "KMS_DSSE"], config.encryption_type) &&
      (config.encryption_type == "AES256" ? config.kms_key == null : try(length(config.kms_key) > 0, false))
    ])
    error_message = "Encryption must be AES256 without a KMS key, or KMS/KMS_DSSE with its existing key."
  }
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

variable "cloudwatch_kms_key_arn" {
  description = "ARN of the KMS key for CloudWatch Log Group encryption (CKV_AWS_158)"
  type        = string
  default     = ""
}
