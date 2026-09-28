variable "environment" {
  type = string
}

variable "name_prefix" {
  type = string
}

variable "oidc_provider_arn" {
  description = "ARN of the shared EKS OIDC provider"
  type        = string
}

variable "oidc_issuer" {
  description = "OIDC issuer URL of the shared EKS cluster"
  type        = string
}

variable "aws_region" {
  type = string
}

variable "runner_namespace" {
  description = "Kubernetes namespace for runner pods"
  type        = string
  default     = "arc-runners"
}

variable "security_scans_bucket_arn" {
  description = "ARN of the security scans S3 bucket for SARIF archival"
  type        = string
  default     = ""
}
