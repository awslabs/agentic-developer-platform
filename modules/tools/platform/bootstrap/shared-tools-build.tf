variable "enable_shared_tools_build" {
  description = "Opt in to the dedicated shared-provider CodeBuild project after the repository and state bucket are approved."
  type        = bool
  default     = false
}

variable "shared_tools_build_bucket" {
  description = "Existing private S3 bucket for reviewed shared-provider build archives and receipts."
  type        = string
  default     = ""
}

variable "shared_tools_lambda_repository" {
  description = "Exact existing shared Lambda ECR repository name (without account or registry)."
  type        = string
  default     = ""
}

variable "shared_tools_browser_repository" {
  description = "Exact existing shared Browser ECR repository name (without account or registry)."
  type        = string
  default     = ""
}

locals {
  shared_build_name = "adp-${var.environment}-shared-tools-build"
  shared_repositories = [for name in [var.shared_tools_lambda_repository, var.shared_tools_browser_repository] :
  "arn:aws:ecr:${var.aws_region}:${var.aws_account_id}:repository/${name}"]
}

resource "aws_cloudwatch_log_group" "shared_build" {
  count             = var.enable_shared_tools_build ? 1 : 0
  name              = "/aws/codebuild/${local.shared_build_name}"
  retention_in_days = 30
}

resource "aws_iam_role" "shared_build" {
  count = var.enable_shared_tools_build ? 1 : 0
  name  = local.shared_build_name
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "codebuild.amazonaws.com" },
    Condition = { StringEquals = {
      "aws:SourceAccount" = var.aws_account_id,
      "aws:SourceArn"     = "arn:aws:codebuild:${var.aws_region}:${var.aws_account_id}:project/${local.shared_build_name}",
    } },
  }] })
}

resource "aws_iam_role_policy" "shared_build" {
  count = var.enable_shared_tools_build ? 1 : 0
  name  = "shared-tools-images-only"
  role  = aws_iam_role.shared_build[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"],
    Resource = "${aws_cloudwatch_log_group.shared_build[0].arn}:*" },
    { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:CompleteLayerUpload", "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart", "ecr:BatchGetImage", "ecr:DescribeImages", "ecr:GetDownloadUrlForLayer"],
    Resource = local.shared_repositories },
    { Effect = "Allow", Action = "s3:GetObject",
    Resource = "arn:aws:s3:::${var.shared_tools_build_bucket}/codebuild/src/${local.shared_build_name}/*" },
    { Effect = "Allow", Action = "s3:PutObject",
    Resource = "arn:aws:s3:::${var.shared_tools_build_bucket}/codebuild/artifacts/shared-agentcore-tools/*" },
  ] })
}

resource "aws_codebuild_project" "shared_build" {
  count         = var.enable_shared_tools_build ? 1 : 0
  name          = local.shared_build_name
  service_role  = aws_iam_role.shared_build[0].arn
  build_timeout = 60

  lifecycle {
    precondition {
      condition = (
        can(regex("^[a-z0-9.-]{3,63}$", var.shared_tools_build_bucket)) &&
        alltrue([for name in [var.shared_tools_lambda_repository, var.shared_tools_browser_repository] : can(regex("^[a-zA-Z0-9_./-]+$", name)) && name != "" && !strcontains(name, "..")]) &&
        var.shared_tools_lambda_repository != var.shared_tools_browser_repository
      )
      error_message = "Coordinator must supply the approved bucket and two distinct existing ECR repositories."
    }
  }

  artifacts { type = "NO_ARTIFACTS" }
  source {
    type      = "S3"
    location  = "${var.shared_tools_build_bucket}/codebuild/src/${local.shared_build_name}/explicit-source-required.zip"
    buildspec = "modules/tools/agentcore/infra/buildspec.yml"
  }
  environment {
    type                        = "LINUX_CONTAINER"
    image                       = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    compute_type                = "BUILD_GENERAL1_MEDIUM"
    privileged_mode             = true
    image_pull_credentials_type = "CODEBUILD"
  }
  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.shared_build[0].name
    }
  }
}

output "shared_tools_build_project_name" {
  value = var.enable_shared_tools_build ? aws_codebuild_project.shared_build[0].name : ""
}
