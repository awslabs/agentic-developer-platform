# Execute generated foundation checks against the already published immutable
# images. This project is separate from image construction and publication: its
# role can pull those two images and write only its own bounded evidence archive.
locals {
  # Official PostgreSQL 16.15, linux/amd64 manifest, verified in the public AWS
  # mirror. This is generated test data only; it never selects a deployed DB.
  foundation_check_postgres   = "public.ecr.aws/docker/library/postgres@sha256:cbf42f0edc946c0bc1b8eeed62403734e1f25dc67a3491455d07a92fdb558b28"
  foundation_check_name       = "${local.repository_name}-foundation-check"
  foundation_check_arn        = "arn:aws:codebuild:${var.aws_region}:${var.aws_account_id}:project/${local.foundation_check_name}"
  foundation_check_source     = "codebuild/src/${local.foundation_check_name}/"
  foundation_check_artifacts  = "codebuild/artifacts/${local.foundation_check_name}/"
  foundation_check_logs       = "arn:aws:logs:${var.aws_region}:${var.aws_account_id}:log-group:/aws/codebuild/${local.foundation_check_name}:*"
  foundation_check_images     = ["arn:aws:ecr:${var.aws_region}:${var.aws_account_id}:repository/adp-gateway", "arn:aws:ecr:${var.aws_region}:${var.aws_account_id}:repository/${local.repository_name}"]
  foundation_check_image_read = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages"]
  foundation_check_actions = concat(local.foundation_check_image_read, [
    "ecr:GetAuthorizationToken", "s3:GetObject", "s3:GetObjectVersion", "s3:PutObject",
    "logs:CreateLogStream", "logs:PutLogEvents",
  ], var.source_kms_key_arn == "" ? [] : ["kms:Decrypt", "kms:GenerateDataKey"])
  foundation_check_policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Sid = "RegistryAuthentication", Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
    { Sid = "ReadReleasedImages", Effect = "Allow", Action = local.foundation_check_image_read, Resource = local.foundation_check_images },
    { Sid = "ReadReviewedSource", Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "arn:aws:s3:::${var.state_bucket}/${local.foundation_check_source}*" },
    { Sid = "WriteOwnEvidence", Effect = "Allow", Action = "s3:PutObject", Resource = "arn:aws:s3:::${var.state_bucket}/${local.foundation_check_artifacts}*" },
    { Sid = "WriteOwnLog", Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = local.foundation_check_logs },
    # These ceilings also fence independent repository/bucket policy grants.
    { Sid = "DenyOtherAPIs", Effect = "Deny", NotAction = local.foundation_check_actions, Resource = "*" },
    { Sid = "DenyOtherImages", Effect = "Deny", Action = local.foundation_check_image_read, NotResource = local.foundation_check_images },
    { Sid = "DenyOtherSource", Effect = "Deny", Action = ["s3:GetObject", "s3:GetObjectVersion"], NotResource = "arn:aws:s3:::${var.state_bucket}/${local.foundation_check_source}*" },
    { Sid = "DenyOtherWrites", Effect = "Deny", Action = "s3:PutObject", NotResource = "arn:aws:s3:::${var.state_bucket}/${local.foundation_check_artifacts}*" },
    { Sid = "DenyOtherLogs", Effect = "Deny", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], NotResource = local.foundation_check_logs },
    ], [for statement in [
      { Sid       = "ArchiveKey", Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = var.source_kms_key_arn,
        Condition = { StringEquals = { "kms:ViaService" = "s3.${var.aws_region}.amazonaws.com", "kms:CallerAccount" = var.aws_account_id } }
      },
      { Sid = "DenyOtherKeys", Effect = "Deny", Action = ["kms:Decrypt", "kms:GenerateDataKey"], NotResource = var.source_kms_key_arn },
      { Sid       = "DenyDirectKeyUse", Effect = "Deny", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = "*",
        Condition = { StringNotEquals = { "kms:ViaService" = "s3.${var.aws_region}.amazonaws.com" } }
      },
  ] : statement if var.source_kms_key_arn != ""]) })
}

resource "aws_cloudwatch_log_group" "foundation_check" {
  name              = "/aws/codebuild/${local.foundation_check_name}"
  retention_in_days = 30
}

resource "aws_iam_role" "foundation_check" {
  name = local.foundation_check_name
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Principal = { Service = "codebuild.amazonaws.com" }, Action = "sts:AssumeRole",
    Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id, "aws:SourceArn" = local.foundation_check_arn } }
  }] })
}

resource "aws_iam_role_policy" "foundation_check" {
  name   = "foundation-runtime-check"
  role   = aws_iam_role.foundation_check.id
  policy = local.foundation_check_policy
}

resource "aws_codebuild_project" "foundation_check" {
  name          = local.foundation_check_name
  description   = "Check released gateway and adapter images with generated isolated fixtures"
  service_role  = aws_iam_role.foundation_check.arn
  build_timeout = 30
  artifacts { type = "NO_ARTIFACTS" }
  source {
    type      = "S3"
    location  = "${var.state_bucket}/${local.foundation_check_source}explicit-source-required.zip"
    buildspec = "codebuild/bs-tools-adapters-foundation-check.yml"
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
      name  = "ADP_FOUNDATION_ARTIFACT_BUCKET"
      value = var.state_bucket
    }
    environment_variable {
      name  = "ADP_FOUNDATION_POSTGRES_IMAGE"
      value = local.foundation_check_postgres
    }
  }
  logs_config {
    cloudwatch_logs { group_name = aws_cloudwatch_log_group.foundation_check.name }
  }
  depends_on = [aws_iam_role_policy.foundation_check, aws_ecr_repository.adapter]
}

output "foundation_check_project_name" { value = aws_codebuild_project.foundation_check.name }
output "foundation_check_project_arn" { value = aws_codebuild_project.foundation_check.arn }
output "foundation_check_role_arn" { value = aws_iam_role.foundation_check.arn }
output "foundation_check_source_prefix" { value = local.foundation_check_source }
output "foundation_check_artifact_prefix" { value = local.foundation_check_artifacts }
output "foundation_check_log_group" { value = aws_cloudwatch_log_group.foundation_check.name }
