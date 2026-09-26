# Conditions below are paired with supported action/resource combinations in
# IAM-MATRIX.md and the checked-in AWS service-reference excerpt. No image tagging
# or deregistration grant exists: post-registration failures retain an obligation.
locals {
  request_caller  = { StringEquals = { "aws:RequestTag/superplane-native-caller" = "$${aws:userid}" } }
  resource_caller = { StringEquals = { "aws:ResourceTag/superplane-native-caller" = "$${aws:userid}" } }
  build_statements = [
    { Sid = "Identity", Effect = "Allow", Action = ["sts:GetCallerIdentity"], Resource = "*" },
    { Sid = "Discovery", Effect = "Allow", Action = ["ec2:DescribeImages", "ec2:DescribeSnapshots", "ec2:DescribeVolumes", "ec2:DescribeInstances", "ec2:DescribeInstanceStatus", "ec2:DescribeInstanceTypes", "ec2:DescribeKeyPairs", "ec2:DescribeRegions", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups", "ec2:DescribeVpcs", "ec2:DescribeNetworkInterfaces", "ec2:DescribeVpcAttribute", "ec2:DescribeTags"], Resource = "*" },
    { Sid = "OwnLogs", Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.native.arn}:*" },
    { Sid = "ImmutableInputRead", Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = ["${aws_s3_bucket.native["input"].arn}/native-input/*", "${aws_s3_bucket.native["input"].arn}/codebuild/src/${var.lane.name}/*"] },
    { Sid = "EvidenceWrite", Effect = "Allow", Action = ["s3:PutObject", "s3:AbortMultipartUpload"], Resource = "${aws_s3_bucket.native["output"].arn}/builds/*" },
    { Sid = "ExactHelperPassRole", Effect = "Allow", Action = ["iam:PassRole"], Resource = aws_iam_role.helper.arn, Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } } },
    { Sid = "HelperProfileRead", Effect = "Allow", Action = ["iam:GetInstanceProfile"], Resource = aws_iam_instance_profile.helper.arn },
    { Sid = "LaunchApprovedImageAndNetwork", Effect = "Allow", Action = ["ec2:RunInstances"], Resource = concat(["${local.image_arn}${var.lane.helper_ami_id}", "${local.ec2_prefix}:subnet/${var.lane.helper_subnet_id}", "${local.ec2_prefix}:security-group/${var.lane.helper_security_group_id}"], [for snapshot in var.lane.source_snapshot_ids : "${local.snapshot_arn}${snapshot}"]) },
    { Sid = "LaunchCallerKey", Effect = "Allow", Action = ["ec2:RunInstances"], Resource = "${local.ec2_prefix}:key-pair/superplane-native-*", Condition = local.resource_caller },
    { Sid = "LaunchCallerInstance", Effect = "Allow", Action = ["ec2:RunInstances"], Resource = "${local.ec2_prefix}:instance/*", Condition = { StringEquals = { "aws:RequestTag/superplane-native-caller" = "$${aws:userid}", "ec2:InstanceType" = var.lane.helper_instance_type, "ec2:InstanceProfile" = aws_iam_instance_profile.helper.arn, "ec2:MetadataHttpTokens" = "required" } } },
    { Sid = "LaunchCallerVolumes", Effect = "Allow", Action = ["ec2:RunInstances"], Resource = "${local.ec2_prefix}:volume/*", Condition = local.request_caller },
    { Sid = "LaunchPrivateCallerInterface", Effect = "Allow", Action = ["ec2:RunInstances"], Resource = "${local.ec2_prefix}:network-interface/*", Condition = { StringEquals = { "aws:RequestTag/superplane-native-caller" = "$${aws:userid}", "ec2:Subnet" = "${local.ec2_prefix}:subnet/${var.lane.helper_subnet_id}" }, Bool = { "ec2:AssociatePublicIpAddress" = "false" } } },
    { Sid = "CreateCallerKey", Effect = "Allow", Action = ["ec2:CreateKeyPair"], Resource = "${local.ec2_prefix}:key-pair/superplane-native-*", Condition = local.request_caller },
    { Sid = "CleanupCallerKey", Effect = "Allow", Action = ["ec2:DeleteKeyPair"], Resource = "${local.ec2_prefix}:key-pair/superplane-native-*", Condition = local.resource_caller },
    { Sid = "StopTerminateCallerHelper", Effect = "Allow", Action = ["ec2:StopInstances", "ec2:TerminateInstances"], Resource = "${local.ec2_prefix}:instance/*", Condition = local.resource_caller },
    { Sid = "HelperEnaOnly", Effect = "Allow", Action = ["ec2:ModifyInstanceAttribute"], Resource = "${local.ec2_prefix}:instance/*", Condition = { StringEquals = { "aws:ResourceTag/superplane-native-caller" = "$${aws:userid}", "ec2:Attribute" = "enaSupport" } } },
    { Sid = "SnapshotCallerVolume", Effect = "Allow", Action = ["ec2:CreateSnapshot"], Resource = "${local.ec2_prefix}:volume/*", Condition = local.resource_caller },
    { Sid = "CreateCallerSnapshot", Effect = "Allow", Action = ["ec2:CreateSnapshot"], Resource = "${local.snapshot_arn}*", Condition = local.request_caller },
    { Sid = "CleanupCallerSnapshot", Effect = "Allow", Action = ["ec2:DeleteSnapshot"], Resource = "${local.snapshot_arn}*", Condition = local.resource_caller },
    { Sid = "CleanupCallerVolume", Effect = "Allow", Action = ["ec2:DeleteVolume"], Resource = "${local.ec2_prefix}:volume/*", Condition = local.resource_caller },
    { Sid = "RegisterFromCallerSnapshot", Effect = "Allow", Action = ["ec2:RegisterImage"], Resource = "${local.snapshot_arn}*", Condition = local.resource_caller },
    { Sid = "RegisterRegionalImage", Effect = "Allow", Action = ["ec2:RegisterImage"], Resource = "${local.image_arn}*" },
    { Sid = "AtomicCallerTags", Effect = "Allow", Action = ["ec2:CreateTags"], Resource = ["${local.ec2_prefix}:instance/*", "${local.ec2_prefix}:volume/*", "${local.ec2_prefix}:network-interface/*", "${local.ec2_prefix}:key-pair/superplane-native-*", "${local.snapshot_arn}*"], Condition = { StringEquals = { "aws:RequestTag/superplane-native-caller" = "$${aws:userid}", "ec2:CreateAction" = ["RunInstances", "CreateSnapshot", "CreateKeyPair"] } } },
    { Sid = "RetagCallerSnapshotOnly", Effect = "Allow", Action = ["ec2:CreateTags"], Resource = "${local.snapshot_arn}*", Condition = { StringEquals = { "aws:ResourceTag/superplane-native-caller" = "$${aws:userid}", "aws:RequestTag/superplane-native-caller" = "$${aws:userid}" } } },
    # CodeBuild VPC ENIs have no producer caller tag. Their IAM isolation is the
    # explicitly supplied dedicated build subnets, not individual build sessions.
    { Sid = "BuildVpcCreateInterface", Effect = "Allow", Action = ["ec2:CreateNetworkInterface"], Resource = concat(["${local.ec2_prefix}:network-interface/*", "${local.ec2_prefix}:security-group/${var.lane.build_security_group_id}"], [for subnet in var.lane.build_subnet_ids : "${local.ec2_prefix}:subnet/${subnet}"]) },
    { Sid = "BuildVpcDeleteInterface", Effect = "Allow", Action = ["ec2:DeleteNetworkInterface"], Resource = "${local.ec2_prefix}:network-interface/*", Condition = { ArnEquals = { "ec2:Subnet" = [for subnet in var.lane.build_subnet_ids : "${local.ec2_prefix}:subnet/${subnet}"] } } },
    { Sid = "BuildVpcInterfacePermission", Effect = "Allow", Action = ["ec2:CreateNetworkInterfacePermission"], Resource = "${local.ec2_prefix}:network-interface/*", Condition = { StringEquals = { "ec2:AuthorizedService" = "codebuild.amazonaws.com" }, ArnEquals = { "ec2:Subnet" = [for subnet in var.lane.build_subnet_ids : "${local.ec2_prefix}:subnet/${subnet}"] } } },
    { Sid = "SelectedKeyRead", Effect = "Allow", Action = ["kms:DescribeKey"], Resource = var.lane.kms_key_arn },
    { Sid = "SelectedKeyViaServices", Effect = "Allow", Action = ["kms:Decrypt", "kms:Encrypt", "kms:ReEncryptFrom", "kms:ReEncryptTo", "kms:GenerateDataKey", "kms:GenerateDataKeyWithoutPlaintext"], Resource = var.lane.kms_key_arn, Condition = { StringEquals = { "kms:ViaService" = ["ec2.${var.lane.region}.amazonaws.com", "s3.${var.lane.region}.amazonaws.com"], "kms:CallerAccount" = var.lane.account_id } } },
    { Sid = "SelectedKeyAwsGrant", Effect = "Allow", Action = ["kms:CreateGrant"], Resource = var.lane.kms_key_arn, Condition = { Bool = { "kms:GrantIsForAWSResource" = "true" }, StringEquals = { "kms:ViaService" = "ec2.${var.lane.region}.amazonaws.com", "kms:CallerAccount" = var.lane.account_id } } }
  ]
  build_policy = {
    Version   = "2012-10-17"
    Statement = concat(local.build_statements, [{ Sid = "RegionalCeiling", Effect = "Deny", Action = ["ec2:*", "kms:*"], Resource = "*", Condition = { StringNotEquals = { "aws:RequestedRegion" = var.lane.region } } }])
  }
}
# AWS managed policies have a 6144-byte document limit. Split the concrete grants;
# the boundary is an action ceiling plus exact non-EC2 resource grants, not a claim
# that IAM permits only one run's generated resource IDs without the identity policy.
locals {
  ec2_ceiling = distinct(flatten([for statement in local.build_statements : [for action in statement.Action : action if startswith(action, "ec2:")]]))
  boundary_policy = {
    Version = "2012-10-17"
    Statement = concat(
      [for statement in local.build_statements : statement if !startswith(statement.Action[0], "ec2:")],
      [{ Sid = "RegionalEc2ActionCeiling", Effect = "Allow", Action = local.ec2_ceiling, Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.lane.region } } }]
    )
  }
}
resource "aws_iam_policy" "build_boundary" {
  name   = "${var.lane.name}-build-boundary"
  policy = jsonencode(local.boundary_policy)
  tags   = local.tags
}
resource "aws_iam_policy" "build" {
  for_each = { for index in range(ceil(length(local.build_statements) / 8)) : tostring(index) => [for offset, statement in local.build_statements : statement if floor(offset / 8) == index] }
  name     = "${var.lane.name}-build-${each.key}"
  policy   = jsonencode({ Version = "2012-10-17", Statement = each.value })
  tags     = local.tags
}
resource "aws_iam_role_policy_attachment" "build" {
  for_each   = aws_iam_policy.build
  role       = aws_iam_role.build.name
  policy_arn = each.value.arn
}
