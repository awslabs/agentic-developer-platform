# =============================================================================
# Knowledge Layer Observability Module — Variables
# =============================================================================

variable "environment" {
  description = "Environment (dev, staging, prod)"
  type        = string
}

variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "name_prefix" {
  description = "Resource name prefix (e.g. adp-dev-agent-context)"
  type        = string
}

variable "tags" {
  description = "Additional tags to apply to all resources"
  type        = map(string)
  default     = {}
}

variable "log_retention_days" {
  description = "CloudWatch log group retention in days"
  type        = number
  default     = 30
}

variable "sqs_queue_name_prefix" {
  description = "SQS queue name prefix for SEARCH() expressions (e.g. adp-dev-eks-cluster-context)"
  type        = string
}

variable "cloudwatch_kms_key_arn" {
  description = "ARN of the KMS key for CloudWatch Log Group encryption (CKV_AWS_158)"
  type        = string
  default     = ""
}
