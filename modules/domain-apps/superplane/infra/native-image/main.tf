# This app-owned root deliberately has no backend defaults. Installation supplies
# its own isolated state/backend and selected AWS provider credentials.
provider "aws" {
  region              = var.lane.region
  allowed_account_ids = [var.lane.account_id]
}
locals {
  tags              = { "superplane-component" = "native-image-lane", "superplane-lane" = var.lane.name }
  ec2_prefix        = "arn:aws:ec2:${var.lane.region}:${var.lane.account_id}"
  image_arn         = "arn:aws:ec2:${var.lane.region}::image/"
  snapshot_arn      = "arn:aws:ec2:${var.lane.region}::snapshot/"
  build_project_arn = "arn:aws:codebuild:${var.lane.region}:${var.lane.account_id}:project/${var.lane.name}"
  constraints = {
    account_id        = var.lane.account_id
    region            = var.lane.region
    helper_ami_id     = var.lane.helper_ami_id
    subnet_id         = var.lane.helper_subnet_id
    security_group_id = var.lane.helper_security_group_id
    instance_profile  = aws_iam_instance_profile.helper.name
    instance_type     = var.lane.helper_instance_type
    kms_key_id        = var.lane.kms_key_arn
  }
}
resource "aws_s3_bucket" "native" {
  for_each      = { input = var.lane.input_bucket_name, output = var.lane.output_bucket_name }
  bucket        = each.value
  force_destroy = false
  tags          = local.tags
}
resource "aws_s3_bucket_public_access_block" "native" {
  for_each                = aws_s3_bucket.native
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_versioning" "native" {
  for_each = aws_s3_bucket.native
  bucket   = each.value.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "native" {
  for_each = aws_s3_bucket.native
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.lane.kms_key_arn
    }
    bucket_key_enabled = true
  }
}
resource "aws_s3_bucket_policy" "native" {
  for_each = aws_s3_bucket.native
  bucket   = each.value.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "RequireTLS", Effect = "Deny", Principal = "*", Action = "s3:*"
      Resource  = [each.value.arn, "${each.value.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}
resource "aws_s3_bucket_lifecycle_configuration" "native" {
  for_each = aws_s3_bucket.native
  bucket   = each.value.id
  rule {
    id     = "review-evidence-retention"
    status = "Enabled"
    filter { prefix = "" }
    expiration { days = var.lane.retention_days }
    noncurrent_version_expiration { noncurrent_days = var.lane.retention_days }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}
resource "aws_cloudwatch_log_group" "native" {
  name              = "/aws/codebuild/${var.lane.name}"
  retention_in_days = 90
  tags              = local.tags
}
resource "aws_iam_role" "helper" {
  name                 = "${var.lane.name}-helper"
  assume_role_policy   = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }] })
  permissions_boundary = aws_iam_policy.helper_boundary.arn
  tags                 = local.tags
}
resource "aws_iam_instance_profile" "helper" {
  name = "${var.lane.name}-helper"
  role = aws_iam_role.helper.name
  tags = local.tags
}
locals {
  helper_policy = {
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["sts:GetCallerIdentity"], Resource = "*" },
      { Effect = "Allow", Action = ["ec2:DescribeVolumes"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.lane.region } } }
    ]
  }
}
resource "aws_iam_policy" "helper_boundary" {
  name   = "${var.lane.name}-helper-boundary"
  policy = jsonencode(local.helper_policy)
  tags   = local.tags
}
resource "aws_iam_role_policy" "helper" {
  role   = aws_iam_role.helper.name
  policy = jsonencode(local.helper_policy)
}
resource "aws_iam_role" "build" {
  name                 = "${var.lane.name}-build"
  permissions_boundary = aws_iam_policy.build_boundary.arn
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "codebuild.amazonaws.com" }, Action = "sts:AssumeRole", Condition = { StringEquals = { "aws:SourceAccount" = var.lane.account_id }, ArnEquals = { "aws:SourceArn" = local.build_project_arn } } }]
  })
  tags = local.tags
}
resource "aws_codebuild_project" "native" {
  name                   = var.lane.name
  service_role           = aws_iam_role.build.arn
  build_timeout          = var.lane.timeout_minutes
  queued_timeout         = 30
  concurrent_build_limit = 1
  source {
    type      = "S3"
    location  = "${aws_s3_bucket.native["input"].id}/codebuild/src/${var.lane.name}/dispatch-required.zip"
    buildspec = "modules/domain-apps/superplane/releases/buildspecs/native-node-lane.yml"
  }
  artifacts { type = "NO_ARTIFACTS" }
  environment {
    type                        = "LINUX_CONTAINER"
    compute_type                = var.lane.compute_type
    image                       = var.lane.environment_image
    image_pull_credentials_type = "CODEBUILD"
    privileged_mode             = true
    dynamic "environment_variable" {
      for_each = {
        AWS_REGION                 = var.lane.region
        NATIVE_ACCOUNT_ID          = var.lane.account_id
        NATIVE_DISPATCHER_ROLE_ARN = var.lane.dispatcher_role_arn
        NATIVE_INPUT_BUCKET        = aws_s3_bucket.native["input"].id
        NATIVE_OUTPUT_BUCKET       = aws_s3_bucket.native["output"].id
        NATIVE_CONSTRAINTS         = jsonencode(local.constraints)
        SUPERPLANE_NATIVE_LANE     = "caller-bound-no-ami-tags"
      }
      content {
        name  = environment_variable.key
        value = environment_variable.value
      }
    }
  }
  vpc_config {
    vpc_id             = var.lane.vpc_id
    subnets            = var.lane.build_subnet_ids
    security_group_ids = [var.lane.build_security_group_id]
  }
  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.native.name
      status     = "ENABLED"
    }
  }
  tags = local.tags
}
