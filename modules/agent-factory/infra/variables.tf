variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment (dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "account_id" {
  description = "AWS account ID (used for remote state bucket name)"
  type        = string
}

variable "github_org" {
  description = "GitHub organization name for runner registration"
  type        = string
}

variable "runner_namespace" {
  description = "Kubernetes namespace for ARC runner pods"
  type        = string
  default     = "arc-runners"
}
