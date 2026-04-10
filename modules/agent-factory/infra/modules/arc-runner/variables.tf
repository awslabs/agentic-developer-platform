variable "environment" {
  type = string
}

variable "name_prefix" {
  type = string
}

variable "cluster_name" {
  type = string
}

variable "runner_namespace" {
  type    = string
  default = "arc-runners"
}

variable "github_org" {
  description = "GitHub organization for runner registration"
  type        = string
}

variable "runner_role_arn" {
  description = "IAM role ARN for runner pod IRSA"
  type        = string
}
