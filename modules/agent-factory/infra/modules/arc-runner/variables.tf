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

variable "github_repo" {
  description = "GitHub repo for repo-scoped runner registration. When set, runners register at the repo level (requires GitHub App 'Administration: Read and write' on the repo). Leave empty for org-scoped."
  type        = string
  default     = ""
}

variable "runner_role_arn" {
  description = "IAM role ARN for runner pod IRSA"
  type        = string
}

variable "github_app_id_secret_name" {
  description = "Secrets Manager secret name holding the GitHub App ID used to authenticate the ARC runner scale set."
  type        = string
}

variable "github_app_private_key_secret_name" {
  description = "Secrets Manager secret name holding the GitHub App private key (PEM)."
  type        = string
}

variable "github_app_installation_id" {
  description = "GitHub App installation ID for the org where runners register."
  type        = string
}
