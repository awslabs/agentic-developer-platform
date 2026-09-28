# Reusable implementation for image jobs owned by each domain app's Terraform
# state. The caller supplies its checked-in project manifest and the shared
# platform's permission boundary; this module never creates core build jobs.
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 6.42.0, < 7.0.0" }
  }
}

variable "domain_app" { type = string }
variable "projects" { type = any }
variable "name_prefix" { type = string }
variable "account_id" { type = string }
variable "aws_region" { type = string }
variable "state_bucket" { type = string }
variable "security_scans_bucket_name" { type = string }
variable "permissions_boundary_arn" { type = string }
variable "common_tags" { type = map(string) }
variable "allowed_artifact_writes" {
  description = "Exact bucket-suffix/prefix pairs that this app may publish"
  type        = set(string)
  default     = []
}

locals {
  repo_prefix = "arn:aws:ecr:${var.aws_region}:${var.account_id}:repository"
  registry    = "${var.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
}

resource "terraform_data" "manifest_guard" {
  input = var.domain_app
  lifecycle {
    precondition {
      condition = alltrue([for name, project in var.projects :
        startswith(name, "${var.domain_app}-") &&
        startswith(project.buildspec, "modules/domain-apps/${var.domain_app}/") &&
        length(project.ecr_repos) > 0 &&
        alltrue([for repo in project.ecr_repos : startswith(repo, "adp-${var.domain_app}-")]) &&
        alltrue([for output in lookup(project, "artifact_writes", []) :
          contains(var.allowed_artifact_writes, "${output.bucket_suffix}/${output.prefix}")
        ]) &&
        project.privileged == true
      ])
      error_message = "Every domain build must use its own buildspec and ECR repository."
    }
  }
}

resource "aws_iam_role" "project" {
  for_each = var.projects

  name        = "${var.name_prefix}-codebuild-${each.key}"
  description = "App-owned image build role for ${each.key}"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "codebuild.amazonaws.com" }, Action = "sts:AssumeRole",
    Condition = { StringEquals = {
      "aws:SourceAccount" = var.account_id,
      "aws:SourceArn"     = "arn:aws:codebuild:${var.aws_region}:${var.account_id}:project/${var.name_prefix}-${each.key}"
    } }
  }] })
  permissions_boundary = var.permissions_boundary_arn
  tags                 = var.common_tags
  depends_on           = [terraform_data.manifest_guard]
}

resource "aws_iam_role_policy" "project" {
  for_each = var.projects
  name     = "build-scope"
  role     = aws_iam_role.project[each.key].id

  policy = jsonencode({ Version = "2012-10-17", Statement = concat(
    [
      {
        Sid    = "OwnBuildLogs", Effect = "Allow",
        Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
        Resource = [
          "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/codebuild/${var.name_prefix}-${each.key}",
          "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/codebuild/${var.name_prefix}-${each.key}:*"
        ]
      },
      {
        Sid      = "BuildSourceRead", Effect = "Allow",
        Action   = ["s3:GetObject", "s3:GetObjectVersion"],
        Resource = "arn:aws:s3:::${var.state_bucket}/codebuild/src/${var.name_prefix}-${each.key}/*"
      },
      {
        Sid    = "EcrAuth", Effect = "Allow",
        Action = ["ecr:GetAuthorizationToken"], Resource = "*"
      },
      {
        Sid = "OwnEcrRepositories", Effect = "Allow",
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload",
          "ecr:DescribeImages", "ecr:DescribeRepositories", "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload", "ecr:ListImages", "ecr:PutImage", "ecr:UploadLayerPart"
        ],
        Resource = [for repo in each.value.ecr_repos : "${local.repo_prefix}/${repo}"]
      },
      {
        Sid      = "EcrRepositoryBootstrap", Effect = "Allow",
        Action   = ["ecr:CreateRepository"],
        Resource = [for repo in each.value.ecr_repos : "${local.repo_prefix}/${repo}"]
      }
    ],
    [for output in lookup(each.value, "artifact_writes", []) : {
      Sid      = "AppArtifactPublish${substr(sha256(output.prefix), 0, 12)}", Effect = "Allow",
      Action   = ["s3:PutObject"],
      Resource = "arn:aws:s3:::${var.name_prefix}-${output.bucket_suffix}/${output.prefix}/*"
    }]
  ) })
}

resource "aws_codebuild_project" "main" {
  for_each      = var.projects
  name          = "${var.name_prefix}-${each.key}"
  description   = "ADP docker build: ${each.key}"
  service_role  = aws_iam_role.project[each.key].arn
  build_timeout = lookup(each.value, "build_timeout", 60)

  artifacts { type = "NO_ARTIFACTS" }
  source {
    type      = "S3"
    location  = "${var.state_bucket}/codebuild/src/${var.name_prefix}-${each.key}/explicit-source-required.zip"
    buildspec = each.value.buildspec
  }
  environment {
    type                        = "LINUX_CONTAINER"
    image                       = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    compute_type                = lookup(each.value, "compute_type", "BUILD_GENERAL1_MEDIUM")
    privileged_mode             = each.value.privileged
    image_pull_credentials_type = "CODEBUILD"
    environment_variable {
      name  = "SECURITY_SCANS_BUCKET"
      value = var.security_scans_bucket_name
    }
    environment_variable {
      name  = "ACCOUNT_ID"
      value = var.account_id
    }
    environment_variable {
      name  = "REGISTRY"
      value = local.registry
    }
  }
  logs_config {
    cloudwatch_logs {
      group_name  = "/aws/codebuild/${var.name_prefix}-${each.key}"
      stream_name = ""
    }
  }
  tags       = var.common_tags
  depends_on = [aws_iam_role_policy.project]
}

output "project_names" {
  value = { for name, project in aws_codebuild_project.main : name => project.name }
}
