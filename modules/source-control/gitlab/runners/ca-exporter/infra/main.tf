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
    error_message = "Select the exact account that owns this GitLab deployment."
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

variable "trusted_operator_role_arns" {
  description = "Existing roles explicitly authorized to assume the separate CA exporter."
  type        = set(string)
  validation {
    condition = length(var.trusted_operator_role_arns) > 0 && alltrue([
      for arn in var.trusted_operator_role_arns :
      can(regex("^arn:aws:iam::${var.account_id}:role/[A-Za-z0-9_+=,.@/-]+$", arn)) &&
      arn != "arn:aws:iam::${var.account_id}:role/adp-${var.environment}-gitlab-runner-ca-exporter"
    ])
    error_message = "Trust must name existing roles in the selected account; empty, wildcard, root, user, session and self principals are not allowed."
  }
}

provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.account_id]
}

# This root owns the identity and trust only. The GitLab instance root attaches
# its certificate/CA-secret policy after creating the actual resources. Keeping
# that policy there avoids speculative secret ARNs and administrator access.
resource "aws_iam_role" "runner_ca_exporter" {
  name                 = "adp-${var.environment}-gitlab-runner-ca-exporter"
  description          = "Separate private GitLab CA exporter; module-scoped permissions attached by the reviewed instance-stack plan."
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRole",
      Principal = { AWS = sort(tolist(var.trusted_operator_role_arns)) }
    }]
  })
  tags = {
    Project   = "adp", Environment = var.environment, Module = "gitlab",
    ManagedBy = "terraform", Purpose = "runner-ca-exporter"
  }
}

output "runner_ca_exporter_role_name" {
  value = aws_iam_role.runner_ca_exporter.name
}

output "runner_ca_exporter_role_arn" {
  value = aws_iam_role.runner_ca_exporter.arn
}
