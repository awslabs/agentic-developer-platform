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
variable "installation_id" { type = string }
variable "management_cluster" { type = string }
variable "namespace" { type = string }
variable "workspace_clusters" {
  type = map(string)
  validation {
    condition     = length(var.workspace_clusters) > 0 && length(var.workspace_clusters) <= 32 && alltrue([for id, name in var.workspace_clusters : can(regex("^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", id)) && can(regex("^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$", name))])
    error_message = "Select bounded explicit workspace UUIDs and existing EKS clusters."
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
}
data "aws_eks_cluster" "management" { name = var.management_cluster }
data "aws_eks_cluster" "workspace" {
  for_each = var.workspace_clusters
  name     = each.value
}
locals {
  issuer    = trimprefix(data.aws_eks_cluster.management.identity[0].oidc[0].issuer, "https://")
  role_name = "adp-${var.environment}-spr-${var.installation_id}"
  tags      = { Project = "adp", Environment = var.environment, DomainApp = "superplane", Installation = var.installation_id, ManagedBy = "terraform" }
}
data "aws_iam_openid_connect_provider" "management" {
  arn = "arn:aws:iam::${var.account_id}:oidc-provider/${local.issuer}"
}
resource "aws_iam_role" "reader" {
  name = local.role_name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = data.aws_iam_openid_connect_provider.management.arn }
      Condition = { StringEquals = {
        "${local.issuer}:sub" = "system:serviceaccount:${var.namespace}:superplane-dedicated-readers"
        "${local.issuer}:aud" = "sts.amazonaws.com"
      } }
    }]
  })
  tags = local.tags
  lifecycle { prevent_destroy = true }
}
resource "aws_iam_role_policy" "reader" {
  name = "describe-installed-workspaces"
  role = aws_iam_role.reader.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["eks:DescribeCluster"], Resource = [for c in data.aws_eks_cluster.workspace : c.arn] },
      { Effect = "Allow", Action = ["eks:DescribeAccessEntry", "eks:ListAssociatedAccessPolicies"], Resource = [for e in aws_eks_access_entry.reader : e.access_entry_arn] }
    ]
  })
}
resource "aws_eks_access_entry" "reader" {
  for_each          = var.workspace_clusters
  cluster_name      = each.value
  principal_arn     = aws_iam_role.reader.arn
  kubernetes_groups = ["superplane:reader:${each.key}"]
  user_name         = "superplane:reader:${each.key}:{{SessionName}}"
  type              = "STANDARD"
  tags              = local.tags
  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = each.value != var.management_cluster && data.aws_eks_cluster.workspace[each.key].access_config[0].authentication_mode == "API"
      error_message = "Dedicated reader requires a distinct workspace using EKS API authentication."
    }
  }
}

# This role has no Kubernetes rights until an actual native bootstrap journal
# installs the exact TokenRequest delegation. No EKS access policy is attached.
output "reader_identity" {
  value = { role_arn = aws_iam_role.reader.arn, role_id = aws_iam_role.reader.unique_id }
}
output "workspace_readers" {
  value = { for id, c in data.aws_eks_cluster.workspace : id => {
    workspace_id     = id, cluster_name = c.name, cluster_arn = c.arn,
    endpoint         = c.endpoint, certificate_authority_data = c.certificate_authority[0].data,
    access_entry_arn = aws_eks_access_entry.reader[id].access_entry_arn
  } }
}
