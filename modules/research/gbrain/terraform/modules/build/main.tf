# =============================================================================
# gbrain Build — CodeBuild project for container image
# =============================================================================
# The gbrain CodeBuild project lives inside the gbrain module's own Terraform
# state (not in platform/infra/modules/codebuild/) so that a single
# `terraform destroy` of the gbrain module removes everything — including the
# build project. This follows the per-module ownership pattern established by
# modules/agent-context/terraform/modules/images-build/.
# =============================================================================

# -----------------------------------------------------------------------------
# IAM — Scoped CodeBuild service role (ECR push + CloudWatch Logs only)
# -----------------------------------------------------------------------------

resource "aws_iam_role" "codebuild" {
  name = "${var.name_prefix}-codebuild-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "sts:AssumeRole"
      Principal = {
        Service = "codebuild.amazonaws.com"
      }
    }]
  })

  tags = var.common_tags
}

resource "aws_iam_role_policy" "codebuild_ecr" {
  name = "ecr-push"
  role = aws_iam_role.codebuild.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ECRAuth"
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken"
        ]
        Resource = "*"
      },
      {
        Sid    = "ECRPush"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:PutImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload"
        ]
        Resource = var.ecr_repo_arn
      }
    ]
  })
}

resource "aws_iam_role_policy" "codebuild_logs" {
  name = "cloudwatch-logs"
  role = aws_iam_role.codebuild.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ]
      Resource = [
        "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/codebuild/${var.name_prefix}-build",
        "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/codebuild/${var.name_prefix}-build:*"
      ]
    }]
  })
}

resource "aws_iam_role_policy" "codebuild_s3_source" {
  name = "s3-source-read"
  role = aws_iam_role.codebuild.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "s3:GetObject",
        "s3:GetObjectVersion"
      ]
      Resource = [
        "arn:aws:s3:::${var.state_bucket}/codebuild/adp-source.zip",
        "arn:aws:s3:::${var.state_bucket}/codebuild/src/*"
      ]
    }]
  })
}

# -----------------------------------------------------------------------------
# CodeBuild Project
# -----------------------------------------------------------------------------

resource "aws_codebuild_project" "gbrain_build" {
  name         = "${var.name_prefix}-build"
  description  = "Build + push gbrain container image to ECR"
  service_role = aws_iam_role.codebuild.arn

  artifacts {
    type = "NO_ARTIFACTS"
  }

  source {
    type      = "S3"
    location  = "${var.state_bucket}/codebuild/adp-source.zip"
    buildspec = "codebuild/bs-gbrain-build.yml"
  }

  environment {
    type                        = "LINUX_CONTAINER"
    image                       = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    compute_type                = "BUILD_GENERAL1_MEDIUM"
    privileged_mode             = true
    image_pull_credentials_type = "CODEBUILD"

    environment_variable {
      name  = "AWS_REGION"
      value = var.aws_region
    }

    environment_variable {
      name  = "REGISTRY"
      value = "${var.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
    }
  }

  build_timeout = 30

  tags = var.common_tags
}
