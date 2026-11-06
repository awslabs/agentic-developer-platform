data "aws_caller_identity" "current" {}

locals {
  oidc_issuer = replace(data.terraform_remote_state.platform.outputs.eks_oidc_provider_arn, "/^arn:aws:iam::[0-9]{12}:oidc-provider\\//", "")
}

resource "aws_iam_role" "manager" {
  for_each = local.group_bindings
  name     = "adp-${var.environment}-gitlab-runner-${each.key}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = data.terraform_remote_state.platform.outputs.eks_oidc_provider_arn }
      Condition = {
        StringEquals = {
          "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
          "${local.oidc_issuer}:sub" = "system:serviceaccount:${each.value.manager_namespace}:gitlab-runner"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "manager" {
  for_each = var.runtime_iam_policies_enabled ? local.group_bindings : {}
  name     = "gitlab-runner-${each.key}"
  role     = aws_iam_role.manager[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "OwnAutoscalingGroup"
        Effect   = "Allow"
        Action   = ["autoscaling:SetDesiredCapacity", "autoscaling:SetInstanceProtection", "autoscaling:TerminateInstanceInAutoScalingGroup", "autoscaling:EnterStandby", "autoscaling:ExitStandby"]
        Resource = aws_autoscaling_group.job[each.key].arn
      },
      {
        Sid       = "RegionalDiscovery"
        Effect    = "Allow"
        Action    = ["autoscaling:DescribeAutoScalingGroups", "ec2:DescribeInstances", "ec2:DescribeSpotInstanceRequests"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
      },
      {
        Sid      = "OwnInstanceConnector"
        Effect   = "Allow"
        Action   = ["ec2-instance-connect:SendSSHPublicKey"]
        Resource = "arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = { StringEquals = {
          "ec2:ResourceTag/aws:autoscaling:groupName" = aws_autoscaling_group.job[each.key].name
          "aws:RequestedRegion"                       = var.aws_region
        } }
      },
      {
        Sid    = "OwnManagerHeartbeat"
        Effect = "Allow"
        Action = ["ssm:PutParameter"]
        Resource = [
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/gitlab-runners/${each.key}/manager-alive",
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/gitlab-runners/${each.key}/registration-receipt"
        ]
      },
      {
        Sid      = "OwnTokenAndSharedCA"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [each.value.token_secret_arn, data.aws_ssm_parameter.ca_secret_arn.value]
      },
      {
        Sid      = "OwnCacheObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.cache.arn}/group/${each.key}/*"
      },
      {
        Sid      = "OwnCacheListing"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.cache.arn
        Condition = { StringLike = {
          "s3:prefix" = ["group/${each.key}/", "group/${each.key}/*"]
        } }
      }
    ]
  })
}
