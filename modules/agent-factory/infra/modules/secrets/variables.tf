variable "environment" {
  type = string
}

variable "name_prefix" {
  type = string
}

variable "github_org" {
  description = "GitHub organization name. Secrets are expected at adp/<org>/gh-app-<role>-{id,key}."
  type        = string
}
