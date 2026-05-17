# =============================================================================
# Agent Context Images Build — Variables
# =============================================================================

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
}

variable "aws_region" {
  description = "AWS region for ECR and CodeBuild"
  type        = string
}

variable "name_prefix" {
  description = "Resource naming prefix (e.g., adp-dev-agent-context)"
  type        = string
}

variable "state_bucket" {
  description = "S3 bucket holding Terraform state and CodeBuild source zips"
  type        = string
}

variable "codebuild_service_role_arn" {
  description = "ARN of the shared CodeBuild IAM role"
  type        = string
}

variable "common_tags" {
  description = "Tags applied to all resources"
  type        = map(string)
  default     = {}
}
