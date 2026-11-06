terraform {
  required_version = ">= 1.9"
  backend "s3" {}
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Select the exact account that owns the GitLab deployment."
  }
}

variable "aws_region" { type = string }

variable "environment" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,31}$", var.environment))
    error_message = "Use a lowercase environment name of 1 to 32 characters."
  }
}

variable "repository" {
  type = string
  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.repository))
    error_message = "Name one exact GitHub owner/repository; wildcards are not allowed."
  }
}

variable "ca_exporter_role_name" {
  description = "Existing separate module CA exporter; empty keeps setup identity-only."
  type        = string
  default     = ""
  validation {
    condition     = var.ca_exporter_role_name == "" || var.ca_exporter_role_name == "adp-${var.environment}-gitlab-runner-ca-exporter"
    error_message = "Delegate only to this environment's existing separate GitLab CA exporter."
  }
}

provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.account_id]
}

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

data "aws_iam_role" "ca_exporter" {
  count = var.ca_exporter_role_name != "" ? 1 : 0
  name  = var.ca_exporter_role_name
}

locals {
  protected_environment = "adp-gitlab-deploy-${var.environment}"
}

# Identity only: creating this role grants no AWS service permissions. The
# instance stack attaches registrar access using real secret ARNs. Subsequent
# saved plans attach deployment permissions only after their bindings exist.
# Keep the CA exporter separate; its own state owns any trust update.
resource "aws_iam_role" "deployment" {
  name                 = "adp-${var.environment}-gitlab-trusted-deployment"
  description          = "GitLab module protected deployment identity; permissions are attached by reviewed module plans."
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "repo:${var.repository}:environment:${local.protected_environment}"
      } }
    }]
  })
  tags = {
    Project   = "adp", Environment = var.environment, Module = "gitlab",
    ManagedBy = "terraform", Purpose = "trusted-deployment"
  }
}

# Opt in only after verifying the exporter exists. Its own root must separately
# trust this exact deployment role; this does not change exporter trust or grant
# direct access to its certificate/CA secret, or to any administrator token.
resource "aws_iam_role_policy" "assume_ca_exporter" {
  count = var.ca_exporter_role_name != "" ? 1 : 0
  name  = "assume-gitlab-ca-exporter"
  role  = aws_iam_role.deployment.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Action = "sts:AssumeRole", Resource = data.aws_iam_role.ca_exporter[0].arn
    }]
  })
}

output "deployment_role_name" { value = aws_iam_role.deployment.name }
output "deployment_role_arn" { value = aws_iam_role.deployment.arn }
output "protected_environment" { value = local.protected_environment }
