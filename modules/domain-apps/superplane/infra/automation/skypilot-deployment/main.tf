# App-owned definitions; composed in the existing automation state.
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}
variable "enable_skypilot_deployment" {
  type    = bool
  default = false
}
variable "name_prefix" { type = string }
variable "repository" { type = string }
variable "environment" { type = string }
variable "aws_region" { type = string }
variable "account_id" { type = string }
variable "cluster_name" { type = string }
variable "github_oidc_provider_arn" { type = string }
resource "aws_iam_role" "skypilot_deployment" {
  count = var.enable_skypilot_deployment ? 1 : 0
  name  = "${var.name_prefix}-skypilot-trusted-deployment"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = var.github_oidc_provider_arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:${var.repository}:environment:adp-skypilot-deploy-${var.environment}"
    } }
  }] })
}
resource "aws_iam_role_policy" "skypilot_deployment" {
  count = var.enable_skypilot_deployment ? 1 : 0
  name  = "release-existing-skypilot"
  role  = aws_iam_role.skypilot_deployment[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["eks:DescribeCluster"], Resource = "arn:aws:eks:${var.aws_region}:${var.account_id}:cluster/${var.cluster_name}" },
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = [for name in [
      "control-plane-role-arn", "skypilot-role-arn", "namespace", "skypilot-namespace", "skypilot-image",
      "database-secret-name", "jwt-secret-name", "workspace-cluster-context", "aws-region"
    ] : "arn:aws:ssm:${var.aws_region}:${var.account_id}:parameter/adp/${var.environment}/superplane/${name}"] }
  ] })
}
resource "aws_eks_access_entry" "skypilot_deployment" {
  count         = var.enable_skypilot_deployment ? 1 : 0
  cluster_name  = var.cluster_name
  principal_arn = aws_iam_role.skypilot_deployment[0].arn
  type          = "STANDARD"
}
resource "aws_eks_access_policy_association" "skypilot_deployment" {
  count         = var.enable_skypilot_deployment ? 1 : 0
  cluster_name  = var.cluster_name
  principal_arn = aws_eks_access_entry.skypilot_deployment[0].principal_arn
  # The Namespace manifest also owns this namespace's pod-security labels.
  # Keep the association namespace-scoped while permitting that object update.
  policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope {
    type       = "namespace"
    namespaces = ["skypilot"]
  }
}

output "deployment_roles" { value = aws_iam_role.skypilot_deployment }
output "deployment_policies" { value = aws_iam_role_policy.skypilot_deployment }
output "access_associations" { value = aws_eks_access_policy_association.skypilot_deployment }
