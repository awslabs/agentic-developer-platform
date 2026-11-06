resource "aws_ssm_parameter" "manager_alive" {
  for_each = local.group_bindings
  name     = "/adp/${var.environment}/gitlab-runners/${each.key}/manager-alive"
  type     = "String"
  value    = "inactive"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "registration_receipt" {
  for_each = local.group_bindings
  name     = "/adp/${var.environment}/gitlab-runners/${each.key}/registration-receipt"
  type     = "String"
  value    = "pending"

  lifecycle {
    ignore_changes = [value]
  }
}

data "aws_iam_openid_connect_provider" "github" {
  arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
}

resource "aws_iam_role" "reconciler" {
  name = "adp-${var.environment}-gitlab-runners-reconciler${local.qualification_suffix}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "repo:aws-e/adp:environment:adp-deploy-${var.environment}"
      } }
    }]
  })
}

moved {
  from = aws_iam_role_policy.reconciler
  to   = aws_iam_role_policy.reconciler[0]
}

resource "aws_iam_role_policy" "reconciler" {
  count = var.runtime_iam_policies_enabled ? 1 : 0
  name  = "runner-owned-instances"
  role  = aws_iam_role.reconciler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "ExactManagerHeartbeats"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = [for group_id in keys(local.group_bindings) : "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/gitlab-runners/${group_id}/manager-alive"]
      },
      {
        Sid       = "RegionalInventory"
        Effect    = "Allow"
        Action    = ["autoscaling:DescribeAutoScalingGroups", "ec2:DescribeInstances", "ec2:DescribeLaunchTemplateVersions"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
      },
      {
        Sid      = "DrainExactGroups"
        Effect   = "Allow"
        Action   = ["autoscaling:SetDesiredCapacity", "autoscaling:SetInstanceProtection"]
        Resource = [for group in aws_autoscaling_group.job : group.arn]
      }
      ], [for group_id, group in aws_autoscaling_group.job : {
        Sid      = "TerminateDetachedOwnedInstances${group_id}"
        Effect   = "Allow"
        Action   = ["ec2:TerminateInstances"]
        Resource = "arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = { StringEquals = {
          "aws:RequestedRegion"              = var.aws_region
          "ec2:ResourceTag/Component"        = "gitlab-job"
          "ec2:ResourceTag/Environment"      = var.environment
          "ec2:ResourceTag/GroupId"          = group_id
          "ec2:ResourceTag/LaunchTemplateId" = aws_launch_template.job[group_id].id
        } }
    }])
  })
}
