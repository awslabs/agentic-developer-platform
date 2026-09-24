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

variable "codebuild_service_role_arns" {
  description = "Dedicated CodeBuild role ARN for each agent-context image key"
  type        = map(string)

  validation {
    condition = alltrue([
      for key in ["ingestion", "codegraph-context", "litellm-proxy", "deepwiki", "context-mcp"] :
      contains(keys(var.codebuild_service_role_arns), key)
    ])
    error_message = "A dedicated CodeBuild role ARN is required for every agent-context image."
  }
}

variable "common_tags" {
  description = "Tags applied to all resources"
  type        = map(string)
  default     = {}
}
