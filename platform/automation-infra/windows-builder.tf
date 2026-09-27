# Windows builds receive their own main-only identity. The ephemeral EC2 role
# has an operator-owned ceiling that this workflow cannot edit or remove.
variable "enable_windows_builder" {
  type    = bool
  default = false
}
variable "windows_builder_subnet_ids" {
  type    = list(string)
  default = []
}
variable "windows_builder_vpc_id" {
  type    = string
  default = ""
}
variable "windows_cape_host_id" {
  type    = string
  default = ""
}
locals {
  windows_prefix       = "adp-${var.environment}-imgbuilder"
  windows_bucket       = "adp-${var.environment}-cape-assets"
  windows_iam          = "arn:aws:iam::${data.aws_caller_identity.current.account_id}"
  windows_ec2          = "arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}"
  windows_role         = "${local.windows_iam}:role/${local.windows_prefix}-builder-role"
  windows_profile      = "${local.windows_iam}:instance-profile/${local.windows_prefix}-builder-profile"
  windows_state_bucket = "adp-terraform-state-${data.aws_caller_identity.current.account_id}"
  windows_state        = "${var.environment}/cyber/image-builder/terraform.tfstate"
  windows_password     = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/cape/builder-windows-password"
}
resource "aws_iam_policy" "windows_builder_boundary" {
  count = var.enable_windows_builder ? 1 : 0
  name  = "${var.name_prefix}-windows-builder-ceiling"
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ssm:UpdateInstanceInformation", "ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel", "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel", "ec2messages:AcknowledgeMessage", "ec2messages:DeleteMessage", "ec2messages:FailMessage", "ec2messages:GetEndpoint", "ec2messages:GetMessages", "ec2messages:SendReply"], Resource = "*" },
    { Effect = "Allow", Action = ["ssm:GetParameter", "ssm:PutParameter", "ssm:DeleteParameter"], Resource = local.windows_password },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"], Resource = ["arn:aws:s3:::${local.windows_bucket}/windows-build-inputs/*", "arn:aws:s3:::${local.windows_bucket}/win11-cape-*", "arn:aws:s3:::${local.windows_bucket}/isos/virtio-win.iso", "arn:aws:s3:::${local.windows_bucket}/isos/win11-enterprise.iso"] },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = "arn:aws:s3:::${local.windows_bucket}" }
  ] })
}
resource "aws_iam_role" "windows_builder" {
  count                = var.enable_windows_builder ? 1 : 0
  name                 = "${var.name_prefix}-windows-trusted-builder"
  max_session_duration = 10800
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:${var.repository}:environment:adp-windows-build-${var.environment}"
    } }
  }] })
  lifecycle {
    precondition {
      condition     = length(var.windows_builder_subnet_ids) > 0 && can(regex("^vpc-[0-9a-f]+$", var.windows_builder_vpc_id)) && can(regex("^i-[0-9a-f]+$", var.windows_cape_host_id))
      error_message = "Windows builds require an explicit existing VPC, subnet inventory and CAPE host."
    }
  }
}
resource "aws_iam_role_policy" "windows_builder" {
  count = var.enable_windows_builder ? 1 : 0
  name  = "bounded-windows-builder-lifecycle"
  role  = aws_iam_role.windows_builder[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "NoCeilingEdits", Effect = "Deny", NotAction = ["iam:Get*", "iam:List*"], Resource = [aws_iam_policy.windows_builder_boundary[0].arn, aws_iam_role.windows_builder[0].arn] },
    { Sid = "NoCeilingRemoval", Effect = "Deny", Action = ["iam:DeleteRolePermissionsBoundary"], Resource = "*" },
    { Sid = "CreateBoundedBuilderRole", Effect = "Allow", Action = ["iam:CreateRole", "iam:PutRolePermissionsBoundary"], Resource = local.windows_role, Condition = { StringEquals = { "iam:PermissionsBoundary" = aws_iam_policy.windows_builder_boundary[0].arn } } },
    { Effect = "Allow", Action = ["iam:GetRole", "iam:ListRolePolicies", "iam:GetRolePolicy", "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole", "iam:ListRoleTags", "iam:TagRole", "iam:UntagRole", "iam:DeleteRole", "iam:PutRolePolicy", "iam:DeleteRolePolicy"], Resource = local.windows_role },
    { Effect = "Allow", Action = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"], Resource = local.windows_role, Condition = { ArnEquals = { "iam:PolicyARN" = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" } } },
    { Effect = "Allow", Action = ["iam:GetInstanceProfile", "iam:CreateInstanceProfile", "iam:DeleteInstanceProfile", "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile", "iam:ListInstanceProfileTags", "iam:TagInstanceProfile", "iam:UntagInstanceProfile"], Resource = local.windows_profile },
    { Effect = "Allow", Action = ["iam:PassRole"], Resource = local.windows_role, Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } } },
    { Effect = "Allow", Action = ["ec2:Describe*"], Resource = "*" },
    # AMIs may advance through Canonical's public SSM parameter, but only their
    # images can launch, in the explicit private subnet inventory below.
    { Sid = "CanonicalImage", Effect = "Allow", Action = ["ec2:RunInstances"], Resource = "arn:aws:ec2:${var.aws_region}::image/*", Condition = { StringEquals = { "ec2:Owner" = "099720109477" } } },
    { Sid = "PrivateSubnets", Effect = "Allow", Action = ["ec2:RunInstances"], Resource = [for id in var.windows_builder_subnet_ids : "${local.windows_ec2}:subnet/${id}"] },
    { Sid = "BuilderSecurityGroup", Effect = "Allow", Action = ["ec2:RunInstances"], Resource = "${local.windows_ec2}:security-group/*", Condition = { StringEquals = { "ec2:ResourceTag/Name" = "${local.windows_prefix}-sg-builder" } } },
    { Sid = "NewInstance", Effect = "Allow", Action = ["ec2:RunInstances"], Resource = "${local.windows_ec2}:instance/*", Condition = { StringEquals = { "aws:RequestTag/Name" = "${local.windows_prefix}-builder", "ec2:InstanceType" = "c8i.4xlarge" }, ArnEquals = { "ec2:InstanceProfile" = local.windows_profile } } },
    { Sid = "EncryptedLaunchStorage", Effect = "Allow", Action = ["ec2:RunInstances"], Resource = "${local.windows_ec2}:volume/*", Condition = { Bool = { "ec2:Encrypted" = "true" } } },
    { Sid = "PrivateLaunchNetwork", Effect = "Allow", Action = ["ec2:RunInstances"], Resource = "${local.windows_ec2}:network-interface/*", Condition = { ArnEquals = { "ec2:Subnet" = [for id in var.windows_builder_subnet_ids : "${local.windows_ec2}:subnet/${id}"] }, Bool = { "ec2:AssociatePublicIpAddress" = "false" } } },
    { Sid = "CreateBuilderSecurityGroup", Effect = "Allow", Action = ["ec2:CreateSecurityGroup"], Resource = "${local.windows_ec2}:security-group/*", Condition = { StringEquals = { "aws:RequestTag/Name" = "${local.windows_prefix}-sg-builder" } } },
    { Effect = "Allow", Action = ["ec2:CreateSecurityGroup"], Resource = "${local.windows_ec2}:vpc/${var.windows_builder_vpc_id}" },
    { Sid = "BuilderNetworkLifecycle", Effect = "Allow", Action = ["ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupEgress", "ec2:DeleteSecurityGroup"], Resource = "${local.windows_ec2}:security-group/*", Condition = { StringEquals = { "ec2:ResourceTag/Name" = "${local.windows_prefix}-sg-builder" } } },
    { Sid = "BuilderInstanceLifecycle", Effect = "Allow", Action = ["ec2:TerminateInstances", "ec2:StopInstances"], Resource = "${local.windows_ec2}:instance/*", Condition = { StringEquals = { "ec2:ResourceTag/Name" = "${local.windows_prefix}-builder" } } },
    { Sid = "TagOnlyAtCreation", Effect = "Allow", Action = ["ec2:CreateTags"], Resource = ["${local.windows_ec2}:instance/*", "${local.windows_ec2}:volume/*", "${local.windows_ec2}:security-group/*", "${local.windows_ec2}:network-interface/*"], Condition = { StringEquals = { "ec2:CreateAction" = ["RunInstances", "CreateSecurityGroup"] } } },
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = "arn:aws:ssm:${var.aws_region}::parameter/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id" },
    { Effect = "Allow", Action = ["ssm:SendCommand"], Resource = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:document/${var.name_prefix}-register-cape-windows", "${local.windows_ec2}:instance/${var.windows_cape_host_id}"] },
    { Effect = "Allow", Action = ["ssm:GetCommandInvocation"], Resource = "*" },
    { Effect = "Allow", Action = ["cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms", "cloudwatch:DescribeAlarms", "cloudwatch:ListTagsForResource", "cloudwatch:TagResource", "cloudwatch:UntagResource"], Resource = "arn:aws:cloudwatch:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alarm:${local.windows_prefix}-builder-idle" },
    { Effect = "Allow", Action = ["s3:GetBucketLocation", "s3:GetBucketTagging", "s3:GetBucketVersioning", "s3:GetEncryptionConfiguration", "s3:GetBucketPublicAccessBlock", "s3:GetLifecycleConfiguration", "s3:GetBucketAcl", "s3:ListBucket"], Resource = "arn:aws:s3:::${local.windows_bucket}" },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = "arn:aws:s3:::${local.windows_bucket}/windows-build-inputs/*" },
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = "arn:aws:s3:::${local.windows_bucket}/win11-cape-*" },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = "arn:aws:s3:::${local.windows_state_bucket}", Condition = { StringLike = { "s3:prefix" = [local.windows_state, "${local.windows_state}.tflock", "env:", "env:/*"] } } },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "arn:aws:s3:::${local.windows_state_bucket}/${local.windows_state}" },
    { Effect = "Allow", Action = ["dynamodb:DescribeTable", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"], Resource = "arn:aws:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/adp-terraform-locks", Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = ["${local.windows_state_bucket}/${local.windows_state}", "${local.windows_state_bucket}/${local.windows_state}-md5"] } } }
  ] })
}

# The builder cannot run arbitrary commands on CAPE. Registration is a reviewed
# operator-owned recipe; its only input is a date, never a shell command or URL.
resource "aws_ssm_document" "windows_registration" {
  count           = var.enable_windows_builder ? 1 : 0
  name            = "${var.name_prefix}-register-cape-windows"
  document_type   = "Command"
  document_format = "JSON"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Register one dated Windows image using the reviewed CAPE recipe"
    parameters = { buildDate = {
      type = "String", allowedPattern = "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
    } }
    mainSteps = [{ action = "aws:runShellScript", name = "registerWindows", inputs = {
      timeoutSeconds = "900"
      runCommand     = ["#!/bin/bash\nset -- '{{buildDate}}'\nexport AWS_REGION='${var.aws_region}' ASSETS_BUCKET='${local.windows_bucket}'\n${file("${path.module}/../../modules/domain-apps/cyber/image-builder/windows/register-cape-vm.sh")}"]
    } }]
  })
}
resource "aws_iam_role_policy" "windows_cape_image_read" {
  count = var.enable_windows_builder ? 1 : 0
  name  = "read-reviewed-windows-images"
  role  = "adp-${var.environment}-cyber-cape-host-role"
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["s3:GetObject"], Resource = "arn:aws:s3:::${local.windows_bucket}/win11-cape-*.qcow2"
  }] })
}
