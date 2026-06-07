variable "name_prefix" {
  description = "Resource name prefix (e.g. adp-research-gbrain)"
  type        = string
}

variable "account_id" {
  description = "AWS account ID"
  type        = string
}

variable "aws_region" {
  description = "AWS region for the CodeBuild project"
  type        = string
}

variable "state_bucket" {
  description = "S3 bucket holding Terraform state and adp-source.zip"
  type        = string
}

variable "ecr_repo_arn" {
  description = "ARN of the ECR repository to push images to"
  type        = string
}

variable "common_tags" {
  description = "Tags applied to all resources (includes ExperimentId for teardown sweep)"
  type        = map(string)
  default     = {}
}
