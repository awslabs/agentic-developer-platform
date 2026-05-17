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

variable "github_repo" {
  description = "GitHub repo for repo-scoped runner registration. Requires the GitHub App to have 'Administration: Read and write' on that repo. Leave empty to use org-scoped."
  type        = string
  default     = ""
}

variable "runner_namespace" {
  description = "Kubernetes namespace for ARC runner pods"
  type        = string
  default     = "arc-runners"
}

variable "gateway_deployed" {
  description = "Set true when the Bedrock gateway module has been applied. Enables the agent-factory to read the gateway's Terraform state for the authorizer Lambda ARN."
  type        = bool
  default     = false
}

variable "github_app_dev_installation_id" {
  description = "GitHub App installation ID for the `dev` persona on the target org. The ARC runner scale set uses this to authenticate runner registration. Find it via: gh api /orgs/<org>/installations --jq '.installations[] | select(.app_slug==\"<org>-adp-agent-dev\") | .id'"
  type        = string
}

variable "runner_image" {
  description = "Container image for ARC runner pods. Built by .github/workflows/arc-runner-build.yml; points at the adp-arc-runner ECR repo. Leave empty to use the chart default (bare actions-runner image)."
  type        = string
  default     = ""
}

variable "enable_public_cfn_bucket" {
  description = "Whether to create the public S3 bucket for CloudFormation templates. Requires account-level S3 Block Public Access to be disabled. Set false in environments with account-level public access blocks."
  type        = bool
  default     = false
}

variable "seed_agent_registry" {
  description = "Whether to seed the agent_registry DDB table with the scaledjob-worker entry. Set false if the item already exists (e.g. from a prior partial apply) and hasn't been imported into state."
  type        = bool
  default     = false
}
