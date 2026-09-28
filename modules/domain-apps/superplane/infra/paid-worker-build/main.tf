# Explicit app-owned root. Never discovered by platform CodeBuild enrollment.
terraform {
  required_version = ">= 1.9, < 2.0"
  backend "s3" {}
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

variable "enabled" {
  type    = bool
  default = false
}
variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Supply the reviewed AWS account ID."
  }
}
variable "region" {
  type = string
  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9]+$", var.region))
    error_message = "Supply the reviewed commercial AWS region."
  }
}
variable "environment" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,19}$", var.environment))
    error_message = "Supply a bounded environment name."
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
}

locals {
  selected    = var.enabled ? toset(["paid"]) : toset([])
  contract    = jsondecode(file("${path.module}/../paid-worker-project.json"))["superplane-paid-worker"]
  project     = "adp-${var.environment}-superplane-paid-worker"
  bucket      = "adp-terraform-state-${var.account_id}"
  project_arn = "arn:aws:codebuild:${var.region}:${var.account_id}:project/${local.project}"
  log_arn     = "arn:aws:logs:${var.region}:${var.account_id}:log-group:/aws/codebuild/${local.project}"
  repo_arn    = "arn:aws:ecr:${var.region}:${var.account_id}:repository/adp-superplane-paid-worker"
  tags        = { "superplane-component" = "paid-worker-build", "Environment" = var.environment }
  build_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "OwnBuildLogs", Effect = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [local.log_arn, "${local.log_arn}:*"]
      },
      {
        Sid      = "BuildSourceRead", Effect = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = ["arn:aws:s3:::${local.bucket}/codebuild/src/${local.project}/*"]
      },
      {
        Sid    = "EcrAuth", Effect = "Allow"
        Action = ["ecr:GetAuthorizationToken"], Resource = ["*"]
      },
      {
        Sid      = "OwnEcrRepository", Effect = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload", "ecr:DescribeImages", "ecr:DescribeRepositories", "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload", "ecr:ListImages", "ecr:PutImage", "ecr:UploadLayerPart"]
        Resource = [local.repo_arn]
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "build" {
  for_each          = local.selected
  name              = "/aws/codebuild/${local.project}"
  retention_in_days = 90
  tags              = local.tags
}
resource "aws_iam_policy" "boundary" {
  for_each = local.selected
  name     = "adp-${var.environment}-superplane-paid-build-boundary"
  policy   = local.build_policy
  tags     = local.tags
}
resource "aws_iam_role" "build" {
  for_each = local.selected
  name     = "adp-${var.environment}-codebuild-superplane-paid-worker"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Principal = { Service = "codebuild.amazonaws.com" }, Action = "sts:AssumeRole"
    Condition = { StringEquals = { "aws:SourceAccount" = var.account_id, "aws:SourceArn" = local.project_arn } }
  }] })
  permissions_boundary = aws_iam_policy.boundary[each.key].arn
  tags                 = local.tags
}
resource "aws_iam_role_policy" "build" {
  for_each = local.selected
  name     = "build-scope"
  role     = aws_iam_role.build[each.key].id
  policy   = local.build_policy
}
resource "aws_codebuild_project" "build" {
  for_each       = local.selected
  name           = local.project
  service_role   = aws_iam_role.build[each.key].arn
  build_timeout  = local.contract.build_timeout
  queued_timeout = 480
  artifacts { type = "NO_ARTIFACTS" }
  source {
    type      = "S3"
    location  = "${local.bucket}/codebuild/src/${local.project}/explicit-source-required.zip"
    buildspec = local.contract.buildspec
  }
  environment {
    type                        = "LINUX_CONTAINER"
    image                       = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    compute_type                = "BUILD_GENERAL1_MEDIUM"
    privileged_mode             = true
    image_pull_credentials_type = "CODEBUILD"
    environment_variable {
      name  = "ACCOUNT_ID"
      value = var.account_id
    }
    environment_variable {
      name  = "REGISTRY"
      value = "${var.account_id}.dkr.ecr.${var.region}.amazonaws.com"
    }
  }
  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.build[each.key].name
    }
  }
  tags       = local.tags
  depends_on = [aws_iam_role_policy.build]
}

output "project_names" {
  value = [for project in aws_codebuild_project.build : project.name]
}
