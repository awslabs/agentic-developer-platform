# Temporary installation-owner authority for native pre-namespace revocation.
# This root owns only its policy on the existing installer role, never that role.
terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}
variable "account_id" { type = string }
variable "region" { type = string }
variable "installer_role_name" {
  type = string
  validation {
    condition     = can(regex("^adp-[a-z0-9-]+-superplane-installer-[a-f0-9]+$", var.installer_role_name))
    error_message = "Use the existing app installer role."
  }
}
variable "entry_arn" {
  type = string
  validation {
    condition     = can(regex("^arn:aws:eks:[a-z0-9-]+:[0-9]{12}:access-entry/[a-zA-Z0-9_-]+/role/[0-9]{12}/[a-zA-Z0-9_+=,.@/-]+/[a-zA-Z0-9-]+$", var.entry_arn))
    error_message = "An exact immutable EKS access-entry ARN is required."
  }
}
variable "expires_at" { type = string }
variable "operation_id" {
  type = string
  validation {
    condition     = can(regex("^[a-f0-9-]{36}$", var.operation_id))
    error_message = "An original operation UUID is required."
  }
}
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
}
resource "aws_iam_role_policy" "cleanup" {
  name = "superplane-registrar-cleanup-${var.operation_id}"
  role = var.installer_role_name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["eks:DeleteAccessEntry", "eks:DisassociateAccessPolicy"]
      Resource = var.entry_arn
      Condition = {
        DateLessThan = { "aws:CurrentTime" = var.expires_at }
      }
    }]
  })
  lifecycle {
    precondition {
      condition     = startswith(var.entry_arn, "arn:aws:eks:${var.region}:${var.account_id}:access-entry/") && strcontains(var.entry_arn, "/role/${var.account_id}/")
      error_message = "The immutable access entry must belong to the selected account and region."
    }
    precondition {
      condition     = timecmp(var.expires_at, plantimestamp()) > 0 && timecmp(var.expires_at, timeadd(plantimestamp(), "2h")) <= 0
      error_message = "Cleanup authorization must expire within two hours of its saved plan."
    }
  }
}
