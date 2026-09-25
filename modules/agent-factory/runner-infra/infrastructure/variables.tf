variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name for resource naming"
  type        = string
  default     = "github-arc-runner"
}

variable "environment" {
  description = "Environment (dev, staging, prod)"
  type        = string
  default     = "prod"
}

variable "cluster_version" {
  description = "EKS cluster version"
  type        = string
  default     = "1.35"
}

variable "github_org" {
  description = "GitHub organization name"
  type        = string
}

variable "vpc_cidr" {
  description = "VPC CIDR block"
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  description = "Availability zones"
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
}

# A18 (#5674): the shared runner role's trust policy used StringLike on
# "system:serviceaccount:arc-runners-*:github-runner-sa", so any namespace whose
# name happened to start "arc-runners-" was trusted by it — including one created
# by onboarding a repository. Trust is now an explicit list: adding a runner
# namespace here is a reviewed change, not a side effect of creating a namespace.
variable "runner_trusted_namespaces" {
  description = "Kubernetes namespaces whose github-runner-sa service account may assume the shared runner role. Exact matches — no patterns. A new runner namespace is untrusted until it is added here and applied."
  type        = list(string)

  # Only "arc-runners" — that is the namespace this stack's own eks.tf binds the
  # shared role to (aws_eks_access_policy_association.runner_edit and
  # kubernetes_cluster_role_binding.runner_namespace_manage). The per-repository
  # "arc-runners-<repo>" namespaces are deliberately NOT here: scripts/
  # onboard-repo.sh gives each repository its OWN role, whose trust policy
  # already names one exact service account and whose inline policy grants only
  # that repository's resources. Listing them here would hand a per-repository
  # runner the shared role's much wider grants — the opposite of onboarding's
  # intent, and what the old "arc-runners-*" pattern did by accident.
  default = ["arc-runners"]

  validation {
    condition     = length(var.runner_trusted_namespaces) > 0
    error_message = "runner_trusted_namespaces must list at least one namespace; an empty list makes the runner role unassumable."
  }

  validation {
    # A "*" or "?" here would recreate the wildcard trust this variable replaced:
    # StringEquals does not glob, so such an entry silently matches nothing and
    # the temptation is to switch the operator back to StringLike.
    condition = alltrue([
      for namespace in var.runner_trusted_namespaces :
      !can(regex("[*?]", namespace))
    ])
    error_message = "runner_trusted_namespaces entries must be exact namespace names, not patterns (A18, #5674). List each namespace individually."
  }
}

locals {
  cluster_name = "${var.project_name}-eks"

  public_subnets  = ["10.0.1.0/24", "10.0.2.0/24"]
  private_subnets = ["10.0.11.0/24", "10.0.12.0/24"]
}

variable "transport_secret_arns" {
  type        = list(string)
  default     = []
  description = "Exact legacy engine transport secret ARNs; validated by the shared runtime policy."
}

variable "transport_secret_kms_arns" {
  type        = list(string)
  default     = []
  description = "Exact KMS key ARNs encrypting the transport secrets; validated by the shared runtime policy."
}

variable "gateway_execution_arns" {
  type        = list(string)
  default     = []
  description = "Reviewed exact API Gateway execution ARNs for runner transport; no API, stage or method wildcards."
}
