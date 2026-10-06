terraform {
  required_version = ">= 1.9"
  backend "s3" {}
  required_providers {
    aws = { source = "hashicorp/aws", version = "6.65.0" }
  }
}

variable "account_id" { type = string }
variable "region" { type = string }
variable "environment" { type = string }
variable "cluster_name" { type = string }
variable "installation_id" { type = string }
variable "operator_role_arn" { type = string }
variable "operator_role_id" { type = string }
variable "namespaces" {
  type = set(string)
  validation {
    condition     = length(var.namespaces) == 2 && alltrue([for n in var.namespaces : can(regex("^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$", n))])
    error_message = "Select exactly the two installation namespaces."
  }
}
variable "installer_policy_json" {
  type        = string
  description = "Reviewed AWS permissions for the actual installation; never copied AdministratorAccess. Saved-plan review must inspect every statement."
  validation {
    condition     = can(jsondecode(var.installer_policy_json).Statement) && !strcontains(var.installer_policy_json, "AdministratorAccess")
    error_message = "Provide the reviewed installation policy document."
  }
}
variable "namespace_bootstrap" {
  type        = bool
  default     = false
  description = "Explicit temporary stage to install app-owned Namespace RBAC. Restore false and apply its reviewed saved plan before running the domain installer."
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
}
data "aws_caller_identity" "operator" {}
data "aws_iam_role" "operator" { name = element(reverse(split("/", var.operator_role_arn)), 0) }
data "aws_eks_cluster" "management" { name = var.cluster_name }

locals {
  role_name = "adp-${var.environment}-superplane-installer-${var.installation_id}"
  group     = "adp:superplane:installer:${var.installation_id}"
  tags      = { Project = "adp", Environment = var.environment, DomainApp = "superplane", Installation = var.installation_id, ManagedBy = "terraform" }
}

resource "aws_iam_role" "installer" {
  name                 = local.role_name
  max_session_duration = 21600
  assume_role_policy   = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { AWS = var.operator_role_arn } }] })
  tags                 = local.tags
  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = data.aws_caller_identity.operator.account_id == var.account_id && data.aws_iam_role.operator.arn == var.operator_role_arn && data.aws_iam_role.operator.unique_id == var.operator_role_id && startswith(data.aws_caller_identity.operator.user_id, "${var.operator_role_id}:")
      error_message = "The Terraform adapter must use the explicitly selected operator role and immutable RoleId."
    }
    precondition {
      condition     = data.aws_eks_cluster.management.arn == "arn:aws:eks:${var.region}:${var.account_id}:cluster/${var.cluster_name}"
      error_message = "Management cluster identity differs from the selected installation."
    }
  }
}

resource "aws_iam_role_policy" "installer" {
  name   = "selected-superplane-installation"
  role   = aws_iam_role.installer.id
  policy = var.installer_policy_json
}
resource "aws_eks_access_entry" "installer" {
  cluster_name      = var.cluster_name
  principal_arn     = aws_iam_role.installer.arn
  kubernetes_groups = [local.group]
  type              = "STANDARD"
  tags              = local.tags
  lifecycle { prevent_destroy = true }
}
resource "aws_eks_access_policy_association" "installer" {
  cluster_name  = var.cluster_name
  principal_arn = aws_eks_access_entry.installer.principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope {
    type       = var.namespace_bootstrap ? "cluster" : "namespace"
    namespaces = var.namespace_bootstrap ? null : sort(concat(tolist(var.namespaces), ["sp-preflight-*"]))
  }
}
resource "aws_eks_access_policy_association" "read" {
  cluster_name  = var.cluster_name
  principal_arn = aws_eks_access_entry.installer.principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy"
  access_scope { type = "cluster" }
}
output "deployment_identity" {
  value = { service = "aws", connection_label = "superplane-installer-${var.installation_id}", expected_role_arn = aws_iam_role.installer.arn, expected_role_id = aws_iam_role.installer.unique_id }
}
output "namespace_rbac_group" { value = local.group }
output "namespace_bootstrap_active" { value = var.namespace_bootstrap }
