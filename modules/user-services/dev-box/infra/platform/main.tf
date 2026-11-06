# Dev Box platform prerequisites (#7998) — the self-managed, platform-account
# half of the module. The foundation stack (../main.tf) assumes three inputs that
# the ADP platform account does not otherwise provide:
#
#   * an SSM interface endpoint security group the guest may reach on 443
#     (the platform VPC has ECR/S3 endpoints only; its SSM endpoints live in
#     another VPC),
#   * a KMS key for the encrypted root and workspace disks (agent-factory keys
#     cover DynamoDB, CloudWatch and Secrets Manager, none of them EBS),
#   * an AMI (no Packer on the operator box, no CodeBuild project for it).
#
# This stack owns those: the endpoints and their group, the workspace key, and a
# CodeBuild project that runs the module's Packer templates (image/*.pkr.hcl) and
# publishes the resulting AMI id to SSM. It is applied first by
# scripts/deploy-dev-box.sh; the foundation stack reads its outputs from SSM
# (/adp/<env>/dev-box/*). State key: <env>/modules/dev-box-platform/terraform.tfstate.
#
# Nothing here is required by the core platform: an environment without the Dev
# Box module never applies this stack.

terraform {
  required_version = ">= 1.7, < 2.0"
  backend "s3" {}
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Supply the twelve-digit platform account ID."
  }
}
variable "region" { type = string }
variable "environment" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,31}$", var.environment))
    error_message = "environment must be a short lowercase name (dev, staging, prod)."
  }
}
variable "vpc_id" { type = string }
variable "vpc_cidr_block" {
  description = "Platform VPC CIDR; the only source allowed to reach the SSM interface endpoints."
  type        = string
  validation {
    condition     = can(cidrhost(var.vpc_cidr_block, 0))
    error_message = "vpc_cidr_block must be a CIDR block."
  }
}
variable "endpoint_subnet_ids" {
  description = "Private subnets that receive the SSM interface endpoints (one per AZ)."
  type        = list(string)
  validation {
    condition     = length(var.endpoint_subnet_ids) > 0 && alltrue([for item in var.endpoint_subnet_ids : can(regex("^subnet-[0-9a-f]{8,17}$", item))])
    error_message = "Supply at least one private subnet ID."
  }
}
variable "build_subnet_id" {
  description = "Private subnet the Packer build instance launches in (must be one of endpoint_subnet_ids)."
  type        = string
  validation {
    condition     = can(regex("^subnet-[0-9a-f]{8,17}$", var.build_subnet_id))
    error_message = "Supply the private subnet ID for image builds."
  }
}
variable "create_ssm_endpoints" {
  description = "Create ssm, ssmmessages and ec2messages interface endpoints in the platform VPC. Set false when the VPC already has them and supply existing_ssm_endpoint_security_group_id."
  type        = bool
  default     = true
}
variable "existing_ssm_endpoint_security_group_id" {
  description = "Security group of pre-existing SSM interface endpoints (used only when create_ssm_endpoints = false)."
  type        = string
  default     = ""
  validation {
    condition     = var.existing_ssm_endpoint_security_group_id == "" || can(regex("^sg-[0-9a-f]{8,17}$", var.existing_ssm_endpoint_security_group_id))
    error_message = "Supply an exact security group ID."
  }
}
variable "state_bucket" {
  description = "Terraform state bucket; CodeBuild reads its per-build source zip and the operator's Crew image inputs from it."
  type        = string
}
variable "image_builder_enabled" {
  description = "Create the CodeBuild project that runs the Packer templates."
  type        = bool
  default     = true
}
variable "image_builder_permissions_boundary_arn" {
  description = "Optional permissions boundary for the CodeBuild role. The platform's shared codebuild boundary denies iam:PassRole, which Packer needs, so leave empty unless a Dev Box-specific boundary exists."
  type        = string
  default     = ""
}
variable "image_build_timeout_minutes" {
  type    = number
  default = 60
}

locals {
  name_prefix = "adp-${var.environment}-dev-box"
  ssm_prefix  = "/adp/${var.environment}/dev-box"
  tags = {
    "adp:component"   = "dev-box"
    "adp:environment" = var.environment
  }
  ssm_endpoint_security_group_id = var.create_ssm_endpoints ? aws_security_group.endpoints[0].id : var.existing_ssm_endpoint_security_group_id
  image_project_name             = "${local.name_prefix}-image"
  image_ssm_parameters = [
    "${local.ssm_prefix}/image-id",
    "${local.ssm_prefix}/image-revision",
    "${local.ssm_prefix}/image-source-revision",
    "${local.ssm_prefix}/base-image-id",
    "${local.ssm_prefix}/profile-revision",
    # Crew bundle publication (image/bundle-buildspec.yml, #8054)
    "${local.ssm_prefix}/crew-bundle-sha256",
    "${local.ssm_prefix}/crew-bundle-revision",
  ]
}

# ---------------------------------------------------------------------------
# Workspace / root disk key
# ---------------------------------------------------------------------------
resource "aws_kms_key" "workspace" {
  description             = "Dev Box ${var.environment}: encrypted guest root and workspace disks"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = local.tags
}

resource "aws_kms_alias" "workspace" {
  name          = "alias/${local.name_prefix}-workspace"
  target_key_id = aws_kms_key.workspace.key_id
}

# ---------------------------------------------------------------------------
# SSM interface endpoints (guest and build instance reach Session Manager
# without a public address or NAT)
# ---------------------------------------------------------------------------
resource "aws_security_group" "endpoints" {
  count                  = var.create_ssm_endpoints ? 1 : 0
  name_prefix            = "${local.name_prefix}-ssm-endpoints-"
  description            = "Dev Box: SSM interface endpoints, HTTPS from the platform VPC only"
  vpc_id                 = var.vpc_id
  revoke_rules_on_delete = true
  tags                   = local.tags
  lifecycle {
    precondition {
      condition     = var.create_ssm_endpoints || var.existing_ssm_endpoint_security_group_id != ""
      error_message = "Either create the SSM endpoints or name the existing endpoint security group."
    }
  }
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_https" {
  count             = var.create_ssm_endpoints ? 1 : 0
  security_group_id = aws_security_group.endpoints[0].id
  cidr_ipv4         = var.vpc_cidr_block
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_endpoint" "ssm" {
  for_each            = var.create_ssm_endpoints ? toset(["ssm", "ssmmessages", "ec2messages"]) : toset([])
  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.endpoint_subnet_ids
  security_group_ids  = [aws_security_group.endpoints[0].id]
  private_dns_enabled = true
  tags                = merge(local.tags, { Name = "${local.name_prefix}-${each.key}" })
}

# ---------------------------------------------------------------------------
# Image builder: CodeBuild runs image/base.pkr.hcl (and image/crew.pkr.hcl when
# the operator supplies an approved Crew bundle + profile) with
# ssh_interface = session_manager, so the temporary build instance needs only
# the SSM endpoints above and an SSM-managed instance profile — plus, for the
# Crew image, read access to the digest-addressed bundle through the VPC's S3
# gateway endpoint (crew.pkr.hcl fetches ~1 GB itself; the Packer file
# provisioner would stream it through the session tunnel into tmpfs).
# ---------------------------------------------------------------------------
resource "aws_iam_role" "image_builder_instance" {
  count       = var.image_builder_enabled ? 1 : 0
  name_prefix = "${local.name_prefix}-image-build-"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" } }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "image_builder_instance_ssm" {
  count      = var.image_builder_enabled ? 1 : 0
  role       = aws_iam_role.image_builder_instance[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "image_builder_instance_inputs" {
  count = var.image_builder_enabled ? 1 : 0
  name  = "crew-bundle-inputs"
  role  = aws_iam_role.image_builder_instance[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadCrewBundleInputs"
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = "arn:aws:s3:::${var.state_bucket}/dev-box/image-inputs/*"
    }]
  })
}

resource "aws_iam_instance_profile" "image_builder_instance" {
  count       = var.image_builder_enabled ? 1 : 0
  name_prefix = "${local.name_prefix}-image-build-"
  role        = aws_iam_role.image_builder_instance[0].name
  tags        = local.tags
}

resource "aws_security_group" "image_builder" {
  count                  = var.image_builder_enabled ? 1 : 0
  name_prefix            = "${local.name_prefix}-image-build-"
  description            = "Dev Box image build instance: SSM endpoints and the S3 gateway endpoint only"
  vpc_id                 = var.vpc_id
  revoke_rules_on_delete = true
  tags                   = local.tags
}

resource "aws_vpc_security_group_egress_rule" "image_builder_ssm" {
  count                        = var.image_builder_enabled ? 1 : 0
  security_group_id            = aws_security_group.image_builder[0].id
  referenced_security_group_id = local.ssm_endpoint_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

data "aws_prefix_list" "s3" {
  count = var.image_builder_enabled ? 1 : 0
  name  = "com.amazonaws.${var.region}.s3"
}

resource "aws_vpc_security_group_egress_rule" "image_builder_s3" {
  count             = var.image_builder_enabled ? 1 : 0
  security_group_id = aws_security_group.image_builder[0].id
  prefix_list_id    = data.aws_prefix_list.s3[0].id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_cloudwatch_log_group" "image_builder" {
  count             = var.image_builder_enabled ? 1 : 0
  name              = "/aws/codebuild/${local.image_project_name}"
  retention_in_days = 90
  tags              = local.tags
}

resource "aws_iam_role" "image_builder" {
  count                = var.image_builder_enabled ? 1 : 0
  name                 = "${local.name_prefix}-image-codebuild"
  permissions_boundary = var.image_builder_permissions_boundary_arn != "" ? var.image_builder_permissions_boundary_arn : null
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "codebuild.amazonaws.com" } }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy" "image_builder" {
  count = var.image_builder_enabled ? 1 : 0
  name  = "packer-build-scope"
  role  = aws_iam_role.image_builder[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.image_builder[0].arn}:*"
      },
      {
        Sid    = "SourceAndInputs"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = [
          "arn:aws:s3:::${var.state_bucket}/codebuild/src/${local.image_project_name}/*",
          "arn:aws:s3:::${var.state_bucket}/dev-box/image-inputs/*",
        ]
      },
      {
        # image/bundle-buildspec.yml publishes the Crew bundle it built under the
        # digest-addressed inputs prefix the Crew image build reads from.
        Sid      = "PublishCrewBundleInputs"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "arn:aws:s3:::${var.state_bucket}/dev-box/image-inputs/*"
      },
      {
        Sid      = "SourceBucket"
        Effect   = "Allow"
        Action   = ["s3:GetBucketLocation", "s3:ListBucket"]
        Resource = "arn:aws:s3:::${var.state_bucket}"
      },
      {
        # Packer amazon-ebs: temporary instance, key pair, AMI and snapshot
        # lifecycle. Limited to the selected region; the build instance itself is
        # constrained by the subnet, security group and instance profile below.
        Sid    = "PackerEc2"
        Effect = "Allow"
        Action = [
          "ec2:AttachVolume", "ec2:AuthorizeSecurityGroupIngress", "ec2:CopyImage", "ec2:CreateImage",
          "ec2:CreateKeyPair", "ec2:CreateSnapshot", "ec2:CreateTags", "ec2:CreateVolume",
          "ec2:DeleteKeyPair", "ec2:DeleteSnapshot", "ec2:DeleteVolume", "ec2:DeregisterImage",
          "ec2:DescribeImageAttribute", "ec2:DescribeImages", "ec2:DescribeInstances",
          "ec2:DescribeInstanceStatus", "ec2:DescribeRegions", "ec2:DescribeSecurityGroups",
          "ec2:DescribeSnapshots", "ec2:DescribeSubnets", "ec2:DescribeTags", "ec2:DescribeVolumes",
          "ec2:DescribeVpcs", "ec2:DetachVolume", "ec2:GetPasswordData", "ec2:ModifyImageAttribute",
          "ec2:ModifyInstanceAttribute", "ec2:ModifySnapshotAttribute", "ec2:RegisterImage",
          "ec2:RunInstances", "ec2:StopInstances", "ec2:TerminateInstances",
        ]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = var.region } }
      },
      {
        Sid       = "PassBuildInstanceRole"
        Effect    = "Allow"
        Action    = ["iam:PassRole", "iam:GetInstanceProfile"]
        Resource  = [aws_iam_role.image_builder_instance[0].arn, aws_iam_instance_profile.image_builder_instance[0].arn]
        Condition = { StringEqualsIfExists = { "iam:PassedToService" = "ec2.amazonaws.com" } }
      },
      {
        Sid      = "SessionManagerTunnel"
        Effect   = "Allow"
        Action   = ["ssm:StartSession"]
        Resource = ["arn:aws:ec2:${var.region}:${var.account_id}:instance/*", "arn:aws:ssm:${var.region}::document/AWS-StartPortForwardingSession"]
      },
      {
        Sid      = "SessionManagerState"
        Effect   = "Allow"
        Action   = ["ssm:TerminateSession", "ssm:ResumeSession", "ssm:DescribeInstanceInformation", "ssm:DescribeSessions", "ssm:GetConnectionStatus"]
        Resource = "*"
      },
      {
        Sid      = "PublicBaseAmiLookup"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = "arn:aws:ssm:${var.region}::parameter/aws/service/ami-amazon-linux-latest/*"
      },
      {
        Sid      = "PublishImageIdentity"
        Effect   = "Allow"
        Action   = ["ssm:PutParameter", "ssm:GetParameter", "ssm:AddTagsToResource"]
        Resource = [for name in local.image_ssm_parameters : "arn:aws:ssm:${var.region}:${var.account_id}:parameter${name}"]
      },
      {
        Sid      = "EncryptBootWithWorkspaceKey"
        Effect   = "Allow"
        Action   = ["kms:CreateGrant", "kms:Decrypt", "kms:DescribeKey", "kms:Encrypt", "kms:GenerateDataKey", "kms:GenerateDataKeyWithoutPlaintext", "kms:ReEncryptFrom", "kms:ReEncryptTo"]
        Resource = aws_kms_key.workspace.arn
      },
    ]
  })
}

resource "aws_codebuild_project" "image" {
  count         = var.image_builder_enabled ? 1 : 0
  name          = local.image_project_name
  description   = "Dev Box AMI build: Packer base + optional Crew profile image, publishes ${local.ssm_prefix}/image-id"
  service_role  = aws_iam_role.image_builder[0].arn
  build_timeout = var.image_build_timeout_minutes
  tags          = local.tags

  artifacts { type = "NO_ARTIFACTS" }

  source {
    type      = "S3"
    location  = "${var.state_bucket}/codebuild/src/${local.image_project_name}/explicit-source-required.zip"
    buildspec = "modules/user-services/dev-box/image/buildspec.yml"
  }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/amazonlinux-x86_64-standard:5.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = false

    environment_variable {
      name  = "ENVIRONMENT"
      value = var.environment
    }
    environment_variable {
      name  = "REGION"
      value = var.region
    }
    environment_variable {
      name  = "ACCOUNT_ID"
      value = var.account_id
    }
    environment_variable {
      name  = "SUBNET_ID"
      value = var.build_subnet_id
    }
    environment_variable {
      name  = "SECURITY_GROUP_ID"
      value = aws_security_group.image_builder[0].id
    }
    environment_variable {
      name  = "BUILD_INSTANCE_PROFILE"
      value = aws_iam_instance_profile.image_builder_instance[0].name
    }
    environment_variable {
      name  = "KMS_KEY_ID"
      value = aws_kms_key.workspace.arn
    }
    environment_variable {
      name  = "SSM_PREFIX"
      value = local.ssm_prefix
    }
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.image_builder[0].name
    }
  }
}

# ---------------------------------------------------------------------------
# Handoff to the foundation stack and the renderers
# ---------------------------------------------------------------------------
resource "aws_ssm_parameter" "kms_key_arn" {
  name  = "${local.ssm_prefix}/kms-key-arn"
  type  = "String"
  value = aws_kms_key.workspace.arn
  tags  = local.tags
}

resource "aws_ssm_parameter" "ssm_endpoint_security_group_id" {
  name  = "${local.ssm_prefix}/ssm-endpoint-security-group-id"
  type  = "String"
  value = local.ssm_endpoint_security_group_id
  tags  = local.tags
}

resource "aws_ssm_parameter" "image_builder_project" {
  name  = "${local.ssm_prefix}/image-builder-project"
  type  = "String"
  value = var.image_builder_enabled ? aws_codebuild_project.image[0].name : " "
  tags  = local.tags
}

output "kms_key_arn" { value = aws_kms_key.workspace.arn }
output "kms_key_alias" { value = aws_kms_alias.workspace.name }
output "ssm_endpoint_security_group_id" { value = local.ssm_endpoint_security_group_id }
output "ssm_endpoint_ids" { value = { for key, endpoint in aws_vpc_endpoint.ssm : key => endpoint.id } }
output "image_builder_project_name" { value = var.image_builder_enabled ? aws_codebuild_project.image[0].name : "" }
output "image_builder_security_group_id" { value = var.image_builder_enabled ? aws_security_group.image_builder[0].id : "" }
output "image_builder_instance_profile" { value = var.image_builder_enabled ? aws_iam_instance_profile.image_builder_instance[0].name : "" }
output "image_ssm_parameters" { value = local.image_ssm_parameters }
