# Service roles get only this ceiling, even if their scoped inline policy/trust is
# changed by the provisioner. In particular there is no IAM, AssumeRole, PassRole,
# secret, backend-state, ELB or EBS mutation grant here.
locals {
  child_reads = [
    "ec2:DescribeInstances", "ec2:DescribeInstanceTypes", "ec2:DescribeTags",
    "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups",
    "ec2:DescribeVolumes", "ec2:DescribeVolumesModifications", "ec2:DescribeRouteTables",
    "ec2:DescribeVpcs", "ec2:DescribeDhcpOptions", "ec2:DescribeAvailabilityZones",
    "ec2:DescribeAccountAttributes", "ec2:DescribeAddresses", "ec2:DescribeInternetGateways",
    "ec2:DescribeInstanceTopology", "autoscaling:DescribeAutoScalingGroups",
    "ecr:GetAuthorizationToken", "eks:ListClusters",
  ]
  child_boundary = {
    Version = "2012-10-17"
    Statement = concat([
      { Effect = "Allow", Action = local.child_reads, Resource = "*", Condition = { StringEquals = local.regional } },
      { Effect = "Allow", Action = ["eks:DescribeCluster", "eks:ListNodegroups", "eks:DescribeNodegroup"], Resource = [local.cluster_arn, "${local.eks_prefix}:nodegroup/${local.workspace_name}/*"] },
      { Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"], Resource = sort(tolist(var.node_image_repository_arns)) },
      # CreateNetworkInterface authorizes three resource kinds independently.
      # Only the new interface uses RequestTag. Existing subnet/SG use their own
      # ownership, so merely supplying tags cannot claim a management network.
      {
        Effect    = "Allow", Action = ["ec2:CreateNetworkInterface"], Resource = "${local.ec2_prefix}:network-interface/*"
        Condition = { StringEquals = local.owned_request }
      },
      {
        Effect    = "Allow", Action = ["ec2:CreateNetworkInterface", "ec2:ModifyNetworkInterfaceAttribute"]
        Resource  = ["${local.ec2_prefix}:subnet/*", "${local.ec2_prefix}:security-group/*"]
        Condition = { StringEquals = local.owned_resource }
      },
      # The EKS-managed node SG has AWS's reserved cluster-name tag rather than
      # Terraform default_tags. A runtime principal cannot forge an aws: tag.
      {
        Effect    = "Allow", Action = ["ec2:CreateNetworkInterface", "ec2:ModifyNetworkInterfaceAttribute"]
        Resource  = "${local.ec2_prefix}:security-group/*"
        Condition = { StringEquals = { "aws:ResourceTag/aws:eks:cluster-name" = local.workspace_name } }
      },
      {
        Effect    = "Allow", Action = ["ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses", "ec2:DeleteNetworkInterface", "ec2:AttachNetworkInterface", "ec2:DetachNetworkInterface", "ec2:ModifyNetworkInterfaceAttribute"]
        Resource  = ["${local.ec2_prefix}:network-interface/*", "${local.ec2_prefix}:instance/*"]
        Condition = { StringEquals = local.owned_resource }
      },
      {
        Effect    = "Allow", Action = ["ec2:CreateTags"], Resource = "${local.ec2_prefix}:network-interface/*"
        Condition = { StringEquals = merge(local.owned_request, { "ec2:CreateAction" = "CreateNetworkInterface" }) }
      },
      # CNI incrementally adds its node/cluster/creation tags to an already-owned
      # primary interface. It cannot first-tag or change either ownership ID.
      {
        Effect    = "Allow", Action = ["ec2:CreateTags"], Resource = "${local.ec2_prefix}:network-interface/*"
        Condition = { StringEquals = local.owned_resource, StringEqualsIfExists = local.owned_request }
      },
      # A resource policy granting directly to a role session can bypass an
      # implicit boundary deny. These explicit denies preserve identity/state
      # isolation even in that case.
      { Effect = "Deny", Action = ["iam:*", "sts:AssumeRole*", "s3:*", "dynamodb:*", "secretsmanager:*", "ssm:*"], Resource = "*" },
      ], var.execution == null ? [] : [
      { Effect = "Allow", Action = ["kms:DescribeKey"], Resource = var.execution.kms_key_arn }
    ])
  }
}
