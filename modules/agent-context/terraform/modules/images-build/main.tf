# =============================================================================
# Agent Context Images Build — ECR + CodeBuild
# =============================================================================
# Creates ECR repositories and CodeBuild projects for the 4 agent-context
# Docker images. Follows the per-module ownership pattern established by
# modules/domain-apps/cyber/infra/ecr.tf.
#
# A single parameterized buildspec (codebuild/bs-agent-context-image.yml) is
# shared across all 4 projects; per-project differences come from CodeBuild
# environment variables (BUILD_DIR, ECR_REPO, AWS_REGION).
# =============================================================================

locals {
  images = {
    "ingestion"         = { build_dir = "modules/agent-context/images/ingestion" }
    "codegraph-context" = { build_dir = "modules/agent-context/images/codegraph-context" }
    "litellm-proxy"     = { build_dir = "modules/agent-context/images/litellm-proxy" }
    "deepwiki"          = { build_dir = "modules/agent-context/images/deepwiki" }
  }
}

# -----------------------------------------------------------------------------
# ECR Repositories
# -----------------------------------------------------------------------------

resource "aws_ecr_repository" "agent_context_images" {
  for_each             = local.images
  name                 = "${var.name_prefix}-${each.key}"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = merge(var.common_tags, {
    Component = "agent-context-images"
    Image     = each.key
  })
}

# -----------------------------------------------------------------------------
# ECR Lifecycle Policies (match cyber-worker pattern: 7-day untagged expiry,
# keep last 10 tagged)
# -----------------------------------------------------------------------------

resource "aws_ecr_lifecycle_policy" "agent_context_images" {
  for_each   = aws_ecr_repository.agent_context_images
  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images older than 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep last 10 tagged images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = { type = "expire" }
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# CodeBuild Projects (one per image, single shared buildspec)
# -----------------------------------------------------------------------------

resource "aws_codebuild_project" "agent_context_images" {
  for_each     = local.images
  name         = "${var.name_prefix}-${each.key}-build"
  description  = "Build + push agent-context/${each.key} image to ECR on Dockerfile change"
  service_role = var.codebuild_service_role_arn

  artifacts {
    type = "NO_ARTIFACTS"
  }

  source {
    type      = "S3"
    location  = "${var.state_bucket}/codebuild/adp-source.zip"
    buildspec = "codebuild/bs-agent-context-image.yml"
  }

  environment {
    type                        = "LINUX_CONTAINER"
    image                       = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    compute_type                = "BUILD_GENERAL1_MEDIUM"
    privileged_mode             = true
    image_pull_credentials_type = "CODEBUILD"

    environment_variable {
      name  = "BUILD_DIR"
      value = each.value.build_dir
    }

    environment_variable {
      name  = "ECR_REPO"
      value = aws_ecr_repository.agent_context_images[each.key].repository_url
    }

    environment_variable {
      name  = "AWS_REGION"
      value = var.aws_region
    }
  }

  build_timeout = 60

  tags = merge(var.common_tags, {
    Component = "agent-context-images"
    Image     = each.key
  })
}
