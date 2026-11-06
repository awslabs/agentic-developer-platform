locals {
  repository_name = "adp-tools-${var.environment}-adapters"
  project_name    = "${local.repository_name}-build"
  project_arn     = "arn:aws:codebuild:${var.aws_region}:${var.aws_account_id}:project/${local.project_name}"
  source_prefix   = "codebuild/src/${local.project_name}/"
  artifact_prefix = "codebuild/artifacts/${local.project_name}/"
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

resource "aws_ecr_repository" "adapter" {
  name                 = local.repository_name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }

  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.aws_account_id && data.aws_region.current.region == var.aws_region
      error_message = "The AWS provider must target the confirmed account and region."
    }
  }
  # Tagged revisions remain available for rollback. Their retention is owned by
  # the release controller, not a generic age-based repository lifecycle rule.
}

resource "aws_cloudwatch_log_group" "build" {
  name              = "/aws/codebuild/${local.project_name}"
  retention_in_days = 30
}

resource "aws_iam_role" "build" {
  name = local.project_name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "codebuild.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = {
        "aws:SourceAccount" = var.aws_account_id,
        "aws:SourceArn"     = local.project_arn,
      } }
    }]
  })
}

resource "aws_iam_role_policy" "build" {
  name = "adapter-image-publication"
  role = aws_iam_role.build.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "ReadReviewedAdapterSource"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = "arn:aws:s3:::${var.state_bucket}/${local.source_prefix}*"
      },
      {
        Sid      = "WriteOnlyOwnQualifiedArchive"
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "arn:aws:s3:::${var.state_bucket}/${local.artifact_prefix}*"
      },
      {
        Sid      = "DenyBuildRegistryMutation"
        Effect   = "Deny"
        Action   = ["ecr:PutImage", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload"]
        Resource = "*"
      },
      {
        Sid      = "WriteOwnBuildLog"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.build.arn}:*"
      },
      ], var.source_kms_key_arn == "" ? [] : [{
        Sid      = "DecryptReviewedSource"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.source_kms_key_arn
        Condition = { StringEquals = {
          "kms:ViaService"    = "s3.${var.aws_region}.amazonaws.com",
          "kms:CallerAccount" = var.aws_account_id,
        } }
      }]
    )
  })
}

resource "aws_codebuild_project" "build" {
  name          = local.project_name
  description   = "Qualify reviewed adapter source without shared registry writes"
  service_role  = aws_iam_role.build.arn
  build_timeout = 30
  artifacts { type = "NO_ARTIFACTS" }
  source {
    type      = "S3"
    location  = "${var.state_bucket}/${local.source_prefix}explicit-source-required.zip"
    buildspec = "codebuild/bs-tools-adapters-build.yml"
  }
  environment {
    type                        = "LINUX_CONTAINER"
    image                       = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    compute_type                = "BUILD_GENERAL1_MEDIUM"
    privileged_mode             = true
    image_pull_credentials_type = "CODEBUILD"
    environment_variable {
      name  = "ACCOUNT_ID"
      value = var.aws_account_id
    }
    environment_variable {
      name  = "ADP_TOOLS_ENVIRONMENT"
      value = var.environment
    }
    environment_variable {
      name  = "ADP_ADAPTER_QUALIFY_ONLY"
      value = "true"
    }
    environment_variable {
      name  = "ADP_ADAPTER_ARTIFACT_BUCKET"
      value = var.state_bucket
    }
  }
  logs_config {
    cloudwatch_logs { group_name = aws_cloudwatch_log_group.build.name }
  }
  depends_on = [aws_iam_role_policy.build]
}
