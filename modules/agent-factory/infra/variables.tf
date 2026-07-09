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
  default     = ""
}

variable "enable_github_apps" {
  description = "Whether GitHub Apps have been created and their secrets stored. When false, the secrets data-source lookups and ARC runner module are skipped (fresh deploy without GitHub Apps)."
  type        = bool
  default     = false
}

variable "runner_image" {
  description = "Container image for ARC runner pods (full URI override). When set, takes precedence over runner_image_repo/runner_image_tag. Leave empty to construct dynamically from caller identity + runner_image_repo + runner_image_tag."
  type        = string
  default     = ""
}

variable "runner_image_repo" {
  description = "ECR repository name for the ARC runner image. Used when runner_image is empty to construct the full URI from the deploying account's ECR."
  type        = string
  default     = "adp-arc-runner"
}

variable "runner_image_tag" {
  description = "Tag for the ARC runner image. Used when runner_image is empty."
  type        = string
  default     = "latest"
}

variable "enable_public_cfn_bucket" {
  description = "Whether to create the public S3 bucket for CloudFormation templates. Requires account-level S3 Block Public Access to be disabled. Set false in environments with account-level public access blocks."
  type        = bool
  default     = false
}

variable "enable_agent_context_rbac" {
  description = "Whether to create runner RBAC resources in the agent-context namespace. Set false when agent-context module is not deployed (namespace doesn't exist)."
  type        = bool
  default     = false
}

variable "seed_agent_registry" {
  description = "Whether to seed the agent_registry DDB table with the scaledjob-worker entry. Defaults true so a fresh deploy registers the worker role automatically (without it, every agent call 500s with UnregisteredServiceAccountError). The resource's lifecycle { ignore_changes = [item] } tolerates drift after creation, so re-applies are safe even where the item already exists outside state."
  type        = bool
  default     = true
}
