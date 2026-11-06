# A separate, disposable owner identity for native acquisition cleanup. The
# original actor trust policies and workspace infrastructure are never changed.
# Destroy all four resources before publishing recovery evidence. The role is
# unique to this failed generation and must never be used by a successor worker.
terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}
variable "account_id" { type = string }
variable "region" { type = string }
variable "cluster_name" { type = string }
variable "generation" {
  type = string
  validation {
    condition     = can(regex("^[a-f0-9]{64}$", var.generation))
    error_message = "The original authority journal generation is required."
  }
}
variable "installer_role_name" {
  type = string
  validation {
    condition     = can(regex("^[A-Za-z0-9+=,.@_-]{1,64}$", var.installer_role_name))
    error_message = "Use an existing IAM role name, without a path."
  }
}
variable "installer_role_id" {
  type        = string
  default     = null
  description = "Immutable RoleId from the protected enrollment, required for an explicitly authorized local installation operator."
  validation {
    condition     = var.installer_role_id == null || can(regex("^AROA[A-Z0-9]{17}$", var.installer_role_id))
    error_message = "The enrolled installation operator must have a genuine immutable IAM RoleId."
  }
}
variable "entry_arns" {
  type = set(string)
  validation {
    condition     = length(var.entry_arns) == 3
    error_message = "Exactly three immutable original actor access-entry ARNs are required."
  }
}
variable "expires_at" { type = string }
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
}
data "aws_iam_role" "installer" {
  count = var.installer_role_id == null ? 0 : 1
  name  = var.installer_role_name
}
data "aws_caller_identity" "installer" {
  count = var.installer_role_id == null ? 0 : 1
}
locals {
  name          = "sp-acquisition-cleanup-${substr(var.generation, 0, 24)}"
  installer_arn = "arn:aws:iam::${var.account_id}:role/${var.installer_role_name}"
  cleanup_arn   = "arn:aws:iam::${var.account_id}:role/sp-acquisition-cleanup-${substr(var.generation, 0, 24)}"
  installer_verified = var.installer_role_id == null ? can(regex("^adp-[a-z0-9-]+-superplane-installer-[a-f0-9]+$", var.installer_role_name)) : (
    data.aws_iam_role.installer[0].arn == local.installer_arn &&
    data.aws_iam_role.installer[0].unique_id == var.installer_role_id &&
    data.aws_caller_identity.installer[0].account_id == var.account_id &&
    split(":", data.aws_caller_identity.installer[0].user_id)[0] == var.installer_role_id &&
    startswith(data.aws_caller_identity.installer[0].arn, "arn:aws:sts::${var.account_id}:assumed-role/${var.installer_role_name}/")
  )
}
resource "aws_iam_role" "cleanup" {
  name                 = local.name
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = local.installer_arn }
      Action    = "sts:AssumeRole"
      Condition = {
        DateLessThan = { "aws:CurrentTime" = var.expires_at }
        StringEquals = { "sts:RoleSessionName" = local.name }
      }
    }]
  })
  tags = { "superplane-cleanup-generation" = var.generation }
  # The enrolled operator need not trust itself for sts:AssumeRole. Native
  # deletion assumes this disposable role with a further per-entry session
  # policy, so EKS cannot delete a replacement at the same principal alias.
  inline_policy {
    name = local.name
    policy = jsonencode({
      Version = "2012-10-17"
      Statement = [{
        Effect    = "Allow"
        Action    = ["eks:DeleteAccessEntry", "eks:DisassociateAccessPolicy"]
        Resource  = sort(tolist(var.entry_arns))
        Condition = { DateLessThan = { "aws:CurrentTime" = var.expires_at } }
      }]
    })
  }
  lifecycle {
    precondition {
      condition     = local.installer_verified
      error_message = "Use the existing app installer or the exact enrolled local operator, verified by IAM RoleId and current STS identity."
    }
    precondition {
      condition     = timecmp(var.expires_at, plantimestamp()) > 0 && timecmp(var.expires_at, timeadd(plantimestamp(), "1h")) <= 0
      error_message = "Cleanup authority must expire within one hour of its saved plan."
    }
  }
}
resource "aws_iam_role_policy" "installer_cleanup" {
  name = local.name
  role = var.installer_role_name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Action    = ["sts:AssumeRole"]
        Resource  = local.cleanup_arn
        Condition = { DateLessThan = { "aws:CurrentTime" = var.expires_at } }
      },
      {
        Effect    = "Allow"
        Action    = ["eks:DeleteAccessEntry", "eks:DisassociateAccessPolicy"]
        Resource  = sort(tolist(var.entry_arns))
        Condition = { DateLessThan = { "aws:CurrentTime" = var.expires_at } }
      }
    ]
  })
  depends_on = [aws_iam_role.cleanup]
  lifecycle {
    precondition {
      condition     = alltrue([for arn in var.entry_arns : startswith(arn, "arn:aws:eks:${var.region}:${var.account_id}:access-entry/${var.cluster_name}/role/${var.account_id}/")])
      error_message = "Every immutable access entry must belong to this exact workspace cluster."
    }
  }
}
resource "aws_eks_access_entry" "cleanup" {
  cluster_name      = var.cluster_name
  principal_arn     = local.cleanup_arn
  type              = "STANDARD"
  user_name         = "${local.name}:{{SessionName}}"
  kubernetes_groups = []
  tags              = { "superplane-cleanup-generation" = var.generation }
  depends_on        = [aws_iam_role.cleanup]
}
resource "aws_eks_access_policy_association" "cleanup" {
  cluster_name  = var.cluster_name
  principal_arn = local.cleanup_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope { type = "cluster" }
  depends_on = [aws_eks_access_entry.cleanup]
}
