# Bootstrap this independent state with an operator identity BEFORE narrowing
# existing runners. Never bootstrap it from the identity it replaces.
terraform {
  required_version = ">= 1.7.0"
  required_providers {
    aws      = { source = "hashicorp/aws", version = "~> 6.0" }
    external = { source = "hashicorp/external", version = "~> 2.3" }
  }
  backend "s3" {}
}

provider "aws" { region = var.aws_region }
data "aws_caller_identity" "current" {}
data "aws_iam_openid_connect_provider" "github" {
  arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
}

variable "name_prefix" { type = string }
variable "environment" { type = string }
variable "aws_region" { type = string }
variable "cluster_name" { type = string }
variable "repository" {
  type = string
  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.repository))
    error_message = "Specify one exact GitHub repository."
  }
}
variable "deployment_secret_arns" {
  type    = list(string)
  default = []
  validation {
    condition     = alltrue([for arn in var.deployment_secret_arns : can(regex("^arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:", arn)) && !can(regex("[*?]", arn))])
    error_message = "Deployment secret reads require exact ARNs, never prefixes."
  }
}

resource "aws_iam_role" "deployment" {
  lifecycle {
    precondition {
      condition     = length(var.deployment_role_boundaries) == 0 ? true : data.external.workload_admission[0].result.verified == "true"
      error_message = "Workload permission ceilings and the complete executable role inventory must pass operator admission."
    }
  }
  name                 = "${var.name_prefix}-trusted-deployment"
  max_session_duration = 10800
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "repo:${var.repository}:environment:adp-deploy-${var.environment}"
      } }
    }]
  })
}

resource "aws_iam_role_policy" "deployment_identity_management" {
  role = aws_iam_role.deployment.id
  name = "reviewed-infrastructure-identity-management"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "ReadDeploymentIdentities", Effect = "Allow",
        Action   = ["iam:GetRole", "iam:GetPolicy", "iam:GetPolicyVersion", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:ListRoleTags", "iam:ListInstanceProfilesForRole", "iam:GetInstanceProfile", "iam:ListPolicyVersions", "iam:ListPolicyTags", "iam:ListInstanceProfileTags"],
        Resource = "*"
      },
      {
        Sid       = "ServiceLinkedRoles", Effect = "Allow", Action = ["iam:CreateServiceLinkedRole"], Resource = "*",
        Condition = { StringEquals = { "iam:AWSServiceName" = ["eks.amazonaws.com", "eks-nodegroup.amazonaws.com", "elasticloadbalancing.amazonaws.com", "rds.amazonaws.com", "autoscaling.amazonaws.com"] } }
      },
      {
        Sid      = "NeverTenantVaults", Effect = "Deny", Action = ["secretsmanager:*"],
        Resource = [for prefix in ["adp/users/", "adp/teams/", "adp/orgs/", "adp/domain-apps/", "adp/*/tenants/"] : "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:${prefix}*"]
      },
      {
        Sid = "ImmutableAutomationAndCeilings", Effect = "Deny", NotAction = ["iam:Get*", "iam:List*"],
        Resource = concat([
          aws_iam_role.deployment.arn, aws_iam_role.frontend_deployment.arn, aws_iam_role.build.arn, aws_iam_role.scan.arn, aws_iam_role.rules.arn, aws_iam_role.checks.arn,
          aws_iam_policy.deployment_base.arn, aws_iam_policy.deployment_services.arn,
        ], [for role in aws_iam_role.browser_checks : role.arn], [for role in aws_iam_role.model_checks : role.arn], [for role in aws_iam_role.chat_deployment : role.arn], [for role in aws_iam_role.context_deployment : role.arn], distinct(values(var.deployment_role_boundaries)), [for policy in aws_iam_policy.deployment_role_lifecycle : policy.arn])
      },
      {
        Sid = "NeverRemoveWorkloadCeilings", Effect = "Deny", Action = ["iam:DeleteRolePermissionsBoundary"], Resource = "*"
      },
      ], length(var.deployment_role_boundaries) == 0 ? [{
        Sid = "AwaitWorkloadAdmission", Effect = "Deny", Action = ["*"], Resource = "*"
    }] : [])
  })
}

resource "aws_eks_access_entry" "deployment" {
  count             = length(var.deployment_role_boundaries) == 0 ? 0 : 1
  cluster_name      = var.cluster_name
  principal_arn     = aws_iam_role.deployment.arn
  kubernetes_groups = ["adp:trusted-deployment"]
  type              = "STANDARD"
}
resource "aws_eks_access_policy_association" "deployment" {
  count         = length(var.deployment_role_boundaries) == 0 ? 0 : 1
  cluster_name  = var.cluster_name
  principal_arn = aws_iam_role.deployment.arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope { type = "cluster" }
  depends_on = [aws_eks_access_entry.deployment]
}
output "deployment_role_arn" { value = aws_iam_role.deployment.arn }
