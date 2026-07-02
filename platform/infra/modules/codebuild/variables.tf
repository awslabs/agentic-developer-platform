variable "name_prefix" {
  description = "Prefix for resource names (e.g. adp-dev)"
  type        = string
}

variable "state_bucket" {
  description = "S3 bucket holding Terraform state and CodeBuild source zips"
  type        = string
}

variable "common_tags" {
  description = "Tags applied to all resources"
  type        = map(string)
  default     = {}
}

variable "security_scans_bucket_arn" {
  description = "ARN of the security scans S3 bucket (for SARIF upload grant)"
  type        = string
}

variable "security_scans_bucket_name" {
  description = "Name of the security scans S3 bucket (passed as env var to CodeBuild)"
  type        = string
}

variable "account_id" {
  description = "AWS account ID (passed as ACCOUNT_ID env var to CodeBuild projects)"
  type        = string
}

variable "ecr_registry" {
  description = "ECR registry URL e.g. 123456789012.dkr.ecr.us-east-1.amazonaws.com (passed as REGISTRY env var to CodeBuild projects)"
  type        = string
}
