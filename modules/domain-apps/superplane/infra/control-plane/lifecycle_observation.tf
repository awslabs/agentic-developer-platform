# API-owned reads for completed workspace apply recovery. No execution sessions.
variable "lifecycle_observation_regions" {
  type    = set(string)
  default = []
  validation {
    condition = length(var.lifecycle_observation_regions) <= 8 && alltrue([
      for r in var.lifecycle_observation_regions : can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]$", r))
    ])
    error_message = "Select at most eight exact recovery observation regions."
  }
}

resource "aws_iam_role" "lifecycle_observer" {
  count = length(var.lifecycle_observation_regions) == 0 ? 0 : 1
  name  = "${local.name_prefix}-lifecycle-observer"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { AWS = "arn:aws:iam::${var.account_id}:role/${local.api_producer_name}" }
    }]
  })
  managed_policy_arns = []
  inline_policy {
    name = "workspace-recovery-reads"
    policy = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid       = "RegionalReads"
          Effect    = "Allow"
          Action    = ["ec2:DescribeAddresses", "ec2:DescribeInstances", "ec2:DescribeInternetGateways", "ec2:DescribeLaunchTemplateVersions", "ec2:DescribeLaunchTemplates", "ec2:DescribeNatGateways", "ec2:DescribeNetworkInterfaces", "ec2:DescribeRouteTables", "ec2:DescribeSecurityGroupRules", "ec2:DescribeSecurityGroups", "ec2:DescribeSubnets", "ec2:DescribeVolumes", "ec2:DescribeVpcEndpoints", "ec2:DescribeVpcs", "eks:DescribeAddon", "eks:DescribeCluster", "eks:DescribeNodegroup", "eks:ListNodegroups", "kms:DescribeKey", "kms:ListAliases", "logs:DescribeLogGroups", "tag:GetResources"]
          Resource  = "*"
          Condition = { StringEquals = { "aws:RequestedRegion" = sort(tolist(var.lifecycle_observation_regions)) } }
        },
        {
          Sid    = "OwnedIdentityReads"
          Effect = "Allow"
          Action = ["iam:GetOpenIDConnectProvider", "iam:GetRole", "iam:GetRolePolicy", "iam:ListAttachedRolePolicies"]
          Resource = [
            "arn:aws:iam::${var.account_id}:role/adp-${var.environment}-spw-*",
            "arn:aws:iam::${var.account_id}:oidc-provider/*"
          ]
        },
        { Effect = "Allow", Action = "sts:GetCallerIdentity", Resource = "*" }
      ]
    })
  }
  lifecycle {
    precondition {
      condition     = var.api_producer_role != null
      error_message = "Lifecycle observer requires the managed API producer identity."
    }
  }
}

output "lifecycle_observation_role_arn" {
  value = try(aws_iam_role.lifecycle_observer[0].arn, null)
}
