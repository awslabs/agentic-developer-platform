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

variable "account_id" { type = string }
variable "region" { type = string }
variable "deployment_id" { type = string }
variable "vpc_id" { type = string }
variable "private_subnet_id" { type = string }
variable "gateway_security_group_id" {
  type = string
  validation {
    condition     = can(regex("^sg-[0-9a-f]{8,17}$", var.gateway_security_group_id))
    error_message = "Supply an exact gateway security group ID, not an ingress CIDR."
  }
}
variable "ssm_endpoint_security_group_id" { type = string }
variable "controller_principal_arn" {
  description = "Principal that may assume the fenced controller role. Empty selects the self-managed IRSA runtime role declared in self_managed.tf (requires eks_oidc_provider_arn)."
  type        = string
  default     = ""
  validation {
    condition     = var.controller_principal_arn == "" || can(regex("^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$", var.controller_principal_arn))
    error_message = "Supply an exact IAM role ARN or leave empty for the self-managed controller runtime."
  }
}
variable "run_table_kms_key_arn" {
  type        = string
  default     = ""
  description = "Customer KMS key that encrypts the run registry table; the controller needs it to mint, renew and revoke host runs. Resolved by scripts/deploy-dev-box.sh from the table's SSE settings."
  validation {
    condition     = var.run_table_kms_key_arn == "" || can(regex("^arn:aws:kms:[a-z]{2}-[a-z]+-[0-9]:[0-9]{12}:key/[0-9a-f-]{36}$", var.run_table_kms_key_arn))
    error_message = "Supply the exact KMS key ARN that encrypts the run registry table, or leave it empty for an AWS-owned key."
  }
}

variable "run_table_arn" {
  type    = string
  default = ""
  validation {
    condition     = var.run_table_arn == "" || can(regex("^arn:aws:dynamodb:[a-z]{2}-[a-z]+-[0-9]:[0-9]{12}:table/[A-Za-z0-9_.-]+$", var.run_table_arn))
    error_message = "The run table must belong to the approved deployment account and region."
  }
}
variable "gateway_execute_arn" {
  type    = string
  default = ""
  validation {
    condition     = var.gateway_execute_arn == "" || can(regex("^arn:aws:execute-api:[a-z]{2}-[a-z]+-[0-9]:[0-9]{12}:[a-z0-9]+/[A-Za-z0-9_-]+$", var.gateway_execute_arn))
    error_message = "Supply the exact gateway API and stage ARN for the selected account and region."
  }
}
variable "image_id" { type = string }
variable "source_revision" { type = string }
variable "template_revision" { type = string }
variable "instance_size" { type = string }
# Capacity fallback (#8423). A launch refused with InsufficientInstanceCapacity (or
# Unsupported: type not offered in that zone) is retried by the controller through
# these admitted sizes in order, and — for a box without a retained workspace disk —
# through these subnets in order. Every entry is admitted by the controller IAM below
# and priced by instance_hour_rates_usd (self_managed.tf); a retained disk pins its
# availability zone, so a replace varies the size only.
variable "fallback_sizes" {
  description = "Ordered instance sizes the controller tries; empty = instance_size followed by the other allowed_sizes."
  type        = list(string)
  default     = []
}
variable "private_subnet_ids" {
  description = "Further approved private subnets (other availability zones) tried after private_subnet_id for a fresh box."
  type        = list(string)
  default     = []
  validation {
    condition     = alltrue([for id in var.private_subnet_ids : can(regex("^subnet-[0-9a-f]{8,17}$", id))])
    error_message = "Supply exact subnet IDs."
  }
}
variable "workspace_disk_gb" { type = number }
variable "kms_key_arn" { type = string }
variable "snapshot_schedule" {
  description = "Daily UTC start time for workspace disk snapshots (HH:MM)."
  type        = string
  default     = "03:00"
  validation {
    condition     = can(regex("^([01][0-9]|2[0-3]):[0-5][0-9]$", var.snapshot_schedule))
    error_message = "snapshot_schedule must be an HH:MM UTC time."
  }
}
variable "snapshot_retention_count" {
  description = "Number of daily workspace snapshots retained by DLM."
  type        = number
  default     = 7
  validation {
    condition     = var.snapshot_retention_count >= 1 && var.snapshot_retention_count <= 1000 && floor(var.snapshot_retention_count) == var.snapshot_retention_count
    error_message = "snapshot_retention_count must be a whole number from 1 to 1000."
  }
}
variable "snapshot_max_gb_per_owner" {
  description = "Admission limit for each owner's snapshot storage; DLM itself does not enforce per-owner limits."
  type        = number
  default     = 1000
  validation {
    condition     = var.snapshot_max_gb_per_owner >= 1 && floor(var.snapshot_max_gb_per_owner) == var.snapshot_max_gb_per_owner
    error_message = "snapshot_max_gb_per_owner must be a positive whole number."
  }
}
variable "dashboard_enabled" {
  type    = bool
  default = false
}

locals {
  resource_prefix = "arn:aws:ec2:${var.region}:${var.account_id}"
  image_arn       = "arn:aws:ec2:${var.region}::image/${var.image_id}"
  # Launch plan (#8423): preferred size first, then the other admitted sizes; the
  # template's subnet first, then the other approved subnets. The IAM statements
  # below admit exactly this plan and nothing else.
  fallback_sizes    = length(var.fallback_sizes) > 0 ? var.fallback_sizes : concat([var.instance_size], [for size in local.allowed_sizes : size if size != var.instance_size])
  launch_subnet_ids = distinct(concat([var.private_subnet_id], var.private_subnet_ids))
  tags = {
    "adp:component"       = "dev-box"
    "adp:deployment-id"   = var.deployment_id
    "adp:source-revision" = var.source_revision
  }
  owned = {
    StringEquals = { "aws:ResourceTag/adp:deployment-id" = var.deployment_id, "aws:ResourceTag/adp:component" = "dev-box" }
    Null         = { "aws:ResourceTag/adp:owner-id" = "false" }
  }
  requested = {
    StringEquals = { "aws:RequestTag/adp:deployment-id" = var.deployment_id, "aws:RequestTag/adp:component" = "dev-box" }
    Null         = { "aws:RequestTag/adp:owner-id" = "false" }
  }
}

# Each approved subnet's availability zone enters the release manifest so the
# controller can keep a replace in the zone of the retained workspace disk (#8423).
data "aws_subnet" "launch" {
  for_each = toset(local.launch_subnet_ids)
  id       = each.value
}

resource "aws_security_group" "guest" {
  name_prefix            = "adp-dev-box-"
  description            = "Dev Box guest: gateway and SSM only"
  vpc_id                 = var.vpc_id
  revoke_rules_on_delete = true
  tags                   = local.tags
}

resource "aws_vpc_security_group_egress_rule" "gateway" {
  security_group_id            = aws_security_group.guest.id
  referenced_security_group_id = var.gateway_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_egress_rule" "ssm" {
  security_group_id            = aws_security_group.guest.id
  referenced_security_group_id = var.ssm_endpoint_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

# The guest's gateway calls (host-restore context/grant/release, model-token refresh,
# model invoke) are SigV4 requests to the gateway's REST API, whose identity check needs
# the API-Gateway-verified caller. That API is a REGIONAL (public) endpoint: a private
# subnet reaches it only through NAT, an execute-api VPC endpoint cannot serve a public
# API (private DNS on such an endpoint disables the default public hostnames), and the
# hostname resolves into the generic EC2 address space of the region, not into the
# published API_GATEWAY ranges (verified from a box, dev 2026-10-19: those ranges are
# API Gateway's own outbound traffic). No destination allowlist exists, so an approved
# gateway adds one HTTPS egress to the internet, the same path the EKS agent pods use.
# The gateway's identity check (registered guest role + controller-written run) remains
# the control on what the box can obtain; decision recorded on #8223.
resource "aws_vpc_security_group_egress_rule" "api_gateway" {
  count             = local.gateway_execute_arn != "" ? 1 : 0
  security_group_id = aws_security_group.guest.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "Gateway REST API (Regional endpoint reached through NAT; #8223)"
}

resource "aws_vpc_security_group_ingress_rule" "dashboard" {
  count                        = local.dashboard_enabled ? 1 : 0
  security_group_id            = aws_security_group.guest.id
  referenced_security_group_id = var.gateway_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 8443
  to_port                      = 8443
}

resource "aws_iam_role" "guest" {
  name_prefix = "adp-dev-box-guest-"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" } }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "guest_ssm" {
  role       = aws_iam_role.guest.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "guest_no_ec2" {
  role = aws_iam_role.guest.id
  name = "deny-guest-ec2-actions"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Deny"
      Action   = ["ec2:*"]
      Resource = "*"
    }]
  })
}

resource "aws_iam_role_policy" "guest_gateway" {
  count = var.run_table_arn != "" && local.gateway_execute_arn != "" ? 1 : 0
  role  = aws_iam_role.guest.id
  name  = "approved-model-route-only"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["execute-api:Invoke"]
      Resource = ["${local.gateway_execute_arn}/POST/agent/model/*/invoke", "${local.gateway_execute_arn}/POST/agent/model/*/invoke-with-response-stream", "${local.gateway_execute_arn}/POST/agent/crew/model-token/refresh", "${local.gateway_execute_arn}/POST/agent/crew/host-restore/context", "${local.gateway_execute_arn}/POST/agent/crew/host-restore/grant", "${local.gateway_execute_arn}/POST/agent/crew/host-restore/release", "${local.gateway_execute_arn}/POST/agent/crew/tools", "${local.gateway_execute_arn}/POST/agent/crew/host-checkpoint/begin", "${local.gateway_execute_arn}/POST/agent/crew/host-checkpoint/complete", "${local.gateway_execute_arn}/POST/agent/crew/github-token", "${local.gateway_execute_arn}/POST/agent/crew/aws-credentials", "${local.gateway_execute_arn}/POST/agent/openai/v1/responses",
        # Owner-scoped host-run reads and inert draft registration for the Dev Box adp CLI (#9485).
        # GET covers products, sources, GitHub status and flow reads; POST is limited to the two
        # draft routes. No gate, execution or amendment route is reachable from a box.
      "${local.gateway_execute_arn}/GET/agent/crew/orchestration/*", "${local.gateway_execute_arn}/POST/agent/crew/orchestration/flows/drafts", "${local.gateway_execute_arn}/POST/agent/crew/orchestration/flows/drafts/preview"]
    }]
  })
  lifecycle {
    precondition {
      condition     = startswith(local.gateway_execute_arn, "arn:aws:execute-api:${var.region}:${var.account_id}:")
      error_message = "The model gateway must be in the selected account and region."
    }
  }
}

resource "aws_iam_instance_profile" "guest" {
  name_prefix = "adp-dev-box-guest-"
  role        = aws_iam_role.guest.name
  tags        = local.tags
}

resource "aws_launch_template" "guest" {
  name_prefix   = "adp-dev-box-"
  image_id      = var.image_id
  instance_type = var.instance_size
  ebs_optimized = true

  iam_instance_profile { arn = aws_iam_instance_profile.guest.arn }
  network_interfaces {
    associate_public_ip_address = false
    security_groups             = [aws_security_group.guest.id]
    subnet_id                   = var.private_subnet_id
  }
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 16
      volume_type           = "gp3"
      encrypted             = true
      kms_key_id            = var.kms_key_arn
      delete_on_termination = true
    }
  }
  tag_specifications {
    resource_type = "instance"
    tags          = local.tags
  }
  tag_specifications {
    resource_type = "volume"
    tags          = local.tags
  }
  # Crew bootstrap (#8055, crew_bootstrap.tf): null in image mode, so the template
  # is exactly what it was; the pinned first-boot script in user-data mode.
  user_data = local.crew_bootstrap_user_data
  tags      = merge(local.tags, { "adp:template-revision" = var.template_revision })
  lifecycle {
    precondition {
      condition     = var.workspace_disk_gb >= 8 && var.workspace_disk_gb <= 16384
      error_message = "A bounded workspace disk is required; controller-created disks remain retained."
    }
    precondition {
      condition     = local.fallback_sizes[0] == var.instance_size && alltrue([for size in local.fallback_sizes : contains(local.allowed_sizes, size)]) && length(distinct(local.fallback_sizes)) == length(local.fallback_sizes)
      error_message = "fallback_sizes must start with instance_size, contain only allowed_sizes and no duplicates (#8423)."
    }
    precondition {
      condition     = !local.crew_user_data_bootstrap || local.crew_bootstrap_inputs_complete
      error_message = "crew_bootstrap = user-data needs crew_bundle_s3_uri, crew_bundle_sha256, crew_source_revision and crew_profile_revision, with the bundle and manifest addressed under dev-box/image-inputs/<crew_bundle_sha256>/."
    }
  }
}

resource "aws_iam_role" "workspace_snapshot_dlm" {
  name_prefix = "adp-dev-box-snapshot-"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "dlm.amazonaws.com" } }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "workspace_snapshot_dlm" {
  role       = aws_iam_role.workspace_snapshot_dlm.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}

resource "aws_iam_role_policy" "workspace_snapshot_kms" {
  name = "selected-ebs-key-only"
  role = aws_iam_role.workspace_snapshot_dlm.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["kms:DescribeKey", "kms:Decrypt", "kms:GenerateDataKeyWithoutPlaintext"], Resource = var.kms_key_arn, Condition = { StringEquals = { "kms:ViaService" = "ec2.${var.region}.amazonaws.com", "kms:CallerAccount" = var.account_id } } },
      { Effect = "Allow", Action = ["kms:CreateGrant"], Resource = var.kms_key_arn, Condition = { Bool = { "kms:GrantIsForAWSResource" = "true" }, StringEquals = { "kms:ViaService" = "ec2.${var.region}.amazonaws.com", "kms:CallerAccount" = var.account_id } } }
    ]
  })
}

resource "aws_dlm_lifecycle_policy" "workspace_snapshot" {
  description        = "Daily encrypted Dev Box workspace snapshots"
  execution_role_arn = aws_iam_role.workspace_snapshot_dlm.arn
  state              = "ENABLED"
  tags               = local.tags

  policy_details {
    resource_types = ["VOLUME"]
    target_tags = {
      "adp:component"     = "dev-box"
      "adp:deployment-id" = var.deployment_id
      "adp:workspace"     = "persistent"
    }

    schedule {
      name      = "daily-workspace"
      copy_tags = true
      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = [var.snapshot_schedule]
      }
      retain_rule {
        count = var.snapshot_retention_count
      }
    }
  }

  depends_on = [aws_iam_role_policy_attachment.workspace_snapshot_dlm, aws_iam_role_policy.workspace_snapshot_kms]
}

resource "aws_iam_role" "controller" {
  name_prefix = "adp-dev-box-controller-"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { AWS = local.controller_principal_arn } }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy" "controller" {
  name = "owned-host-lifecycle"
  role = aws_iam_role.controller.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "Inventory", Effect = "Allow", Action = ["ec2:DescribeInstances", "ec2:DescribeVolumes", "ec2:DescribeSnapshots", "ec2:DescribeImages", "ssm:DescribeInstanceInformation"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.region } } },
      # RunInstances through a launch template authorizes the template itself as a
      # resource (first live launch on dev was refused on launch-template/lt-…).
      { Sid = "LaunchApprovedInputs", Effect = "Allow", Action = ["ec2:RunInstances"], Resource = concat([local.image_arn], [for id in local.launch_subnet_ids : "${local.resource_prefix}:subnet/${id}"], [aws_security_group.guest.arn, aws_launch_template.guest.arn]) },
      { Sid = "LaunchOwnedInstance", Effect = "Allow", Action = ["ec2:RunInstances"], Resource = "${local.resource_prefix}:instance/*", Condition = merge(local.requested, { StringEquals = merge(local.requested.StringEquals, { "ec2:InstanceType" = local.fallback_sizes, "ec2:InstanceProfile" = aws_iam_instance_profile.guest.arn, "ec2:MetadataHttpTokens" = "required" }) }) },
      { Sid = "LaunchOwnedInterface", Effect = "Allow", Action = ["ec2:RunInstances"], Resource = "${local.resource_prefix}:network-interface/*", Condition = merge(local.requested, { Bool = { "ec2:AssociatePublicIpAddress" = "false" } }) },
      { Sid = "LaunchOwnedVolume", Effect = "Allow", Action = ["ec2:RunInstances"], Resource = "${local.resource_prefix}:volume/*", Condition = local.requested },
      { Sid = "CreateOwnedDisk", Effect = "Allow", Action = ["ec2:CreateVolume"], Resource = "${local.resource_prefix}:volume/*", Condition = merge(local.requested, { Bool = { "ec2:Encrypted" = "true" }, NumericLessThanEquals = { "ec2:VolumeSize" = var.workspace_disk_gb }, NumericGreaterThanEquals = { "ec2:VolumeSize" = 8 } }) },
      { Sid = "RestoreOwnedSnapshot", Effect = "Allow", Action = ["ec2:CreateVolume"], Resource = "${local.resource_prefix}:snapshot/*", Condition = local.owned },
      { Sid = "OperateOwnedInstances", Effect = "Allow", Action = ["ec2:StartInstances", "ec2:StopInstances", "ec2:TerminateInstances"], Resource = "${local.resource_prefix}:instance/*", Condition = local.owned },
      { Sid = "OperateOwnedDisks", Effect = "Allow", Action = ["ec2:AttachVolume", "ec2:DetachVolume", "ec2:DeleteVolume"], Resource = "${local.resource_prefix}:volume/*", Condition = local.owned },
      # #8835 disk growth: enlarge (and move to gp3) an owned retained disk in place; the controller bounds the size by the policy's max_disk_gb.
      { Sid = "GrowOwnedDisk", Effect = "Allow", Action = ["ec2:ModifyVolume"], Resource = "${local.resource_prefix}:volume/*", Condition = local.owned },
      { Sid = "OperateOwnedAttachments", Effect = "Allow", Action = ["ec2:AttachVolume", "ec2:DetachVolume"], Resource = "${local.resource_prefix}:instance/*", Condition = local.owned },
      { Sid = "SendOwnedCommand", Effect = "Allow", Action = ["ssm:SendCommand"], Resource = "arn:aws:ssm:${var.region}::document/AWS-RunShellScript" },
      { Sid = "SendOwnedInstanceCommand", Effect = "Allow", Action = ["ssm:SendCommand"], Resource = "${local.resource_prefix}:instance/*", Condition = local.owned },
      { Sid = "ReadCommandResult", Effect = "Allow", Action = ["ssm:GetCommandInvocation"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.region } } },
      { Sid = "TagOnCreation", Effect = "Allow", Action = ["ec2:CreateTags"], Resource = ["${local.resource_prefix}:instance/*", "${local.resource_prefix}:volume/*", "${local.resource_prefix}:network-interface/*"], Condition = merge(local.requested, { StringEquals = merge(local.requested.StringEquals, { "ec2:CreateAction" = ["RunInstances", "CreateVolume"] }) }) },
      { Sid = "PassOnlyGuest", Effect = "Allow", Action = ["iam:PassRole"], Resource = aws_iam_role.guest.arn, Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } } }
    ]
  })
}

output "launch_template_id" { value = aws_launch_template.guest.id }
output "guest_security_group_id" { value = aws_security_group.guest.id }
output "guest_role_arn" { value = aws_iam_role.guest.arn }
resource "aws_iam_role_policy" "controller_run_registry" {
  count = var.run_table_arn == "" ? 0 : 1
  name  = "owner-bound-host-runs"
  role  = aws_iam_role.controller.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:DeleteItem"]
        Resource = var.run_table_arn
      }],
      # A run registry encrypted with a customer KMS key needs the key for every item write and
      # read; without it the controller's mint fails with AccessDeniedException on TransactWriteItems
      # (seen live 2026-10-18) and host enrollment never completes. Scoped to use via DynamoDB only.
      var.run_table_kms_key_arn == "" ? [] : [{
        Effect    = "Allow"
        Action    = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource  = var.run_table_kms_key_arn
        Condition = { StringEquals = { "kms:ViaService" = "dynamodb.${var.region}.amazonaws.com" } }
      }]
    )
  })
  lifecycle {
    precondition {
      condition     = startswith(var.run_table_arn, "arn:aws:dynamodb:${var.region}:${var.account_id}:table/")
      error_message = "Run registry must belong to the approved account and region."
    }
    precondition {
      condition     = var.run_table_kms_key_arn == "" || startswith(var.run_table_kms_key_arn, "arn:aws:kms:${var.region}:${var.account_id}:key/")
      error_message = "The run registry KMS key must belong to the approved account and region."
    }
  }
}

output "run_table_arn" { value = var.run_table_arn }
output "controller_role_arn" { value = aws_iam_role.controller.arn }
output "image_id" { value = var.image_id }
output "source_revision" { value = var.source_revision }
output "template_revision" { value = var.template_revision }
output "workspace_disk_gb" { value = var.workspace_disk_gb }

resource "aws_iam_role_policy" "controller_kms" {
  name = "selected-ebs-key-only"
  role = aws_iam_role.controller.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # EC2 pre-validates the launching principal's EBS key permissions before any KMS
      # call; without ReEncrypt* an instance launched from the CMK-encrypted AMI is
      # terminated with Client.InvalidKMSKey.InvalidState (third live launch on dev).
      { Effect = "Allow", Action = ["kms:DescribeKey", "kms:Decrypt", "kms:GenerateDataKeyWithoutPlaintext", "kms:ReEncryptFrom", "kms:ReEncryptTo"], Resource = var.kms_key_arn, Condition = { StringEquals = { "kms:ViaService" = "ec2.${var.region}.amazonaws.com", "kms:CallerAccount" = var.account_id } } },
      { Effect = "Allow", Action = ["kms:CreateGrant"], Resource = var.kms_key_arn, Condition = { Bool = { "kms:GrantIsForAWSResource" = "true" }, StringEquals = { "kms:ViaService" = "ec2.${var.region}.amazonaws.com", "kms:CallerAccount" = var.account_id } } }
    ]
  })
}

output "launch_template_version" { value = aws_launch_template.guest.latest_version }
output "fallback_sizes" { value = local.fallback_sizes }
output "launch_subnet_ids" { value = local.launch_subnet_ids }
output "guest_instance_profile_arn" { value = aws_iam_instance_profile.guest.arn }
