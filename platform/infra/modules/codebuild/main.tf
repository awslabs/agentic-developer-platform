# =============================================================================
# CodeBuild Projects — docker-requiring builds only
# =============================================================================
# Only builds that need `privileged_mode = true` (i.e. `docker build`) belong
# here. Everything else (Terraform apply, npm build, kubectl apply) runs
# directly on the ARC runner.
#
# TODO: Replace AdministratorAccess with a scoped policy (ECR push, S3 read,
#       CloudWatch Logs, EKS describe, Secrets Manager read). Tracked separately.
# =============================================================================

locals {
  projects = {
    "gateway-build" = { buildspec = "codebuild/bs-gateway-build.yml" }
    "chat-agent"    = { buildspec = "codebuild/bs-chat-agent.yml" }
    "agent-gateway" = { buildspec = "codebuild/bs-agent-gateway.yml" }
    "arc-runner"    = { buildspec = "codebuild/bs-arc-runner.yml" }
  }
}

# -----------------------------------------------------------------------------
# Shared IAM role for all CodeBuild projects
# -----------------------------------------------------------------------------
data "aws_iam_policy_document" "codebuild_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "codebuild" {
  name               = "${var.name_prefix}-codebuild-role"
  assume_role_policy = data.aws_iam_policy_document.codebuild_assume.json
  tags               = var.common_tags
}

# TODO: Scope this down — see module header comment.
resource "aws_iam_role_policy_attachment" "codebuild_admin" {
  role       = aws_iam_role.codebuild.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# -----------------------------------------------------------------------------
# CodeBuild projects (one per docker-build workflow)
# -----------------------------------------------------------------------------
resource "aws_codebuild_project" "main" {
  for_each = local.projects

  name         = "${var.name_prefix}-${each.key}"
  description  = "ADP docker build: ${each.key}"
  service_role = aws_iam_role.codebuild.arn

  artifacts {
    type = "NO_ARTIFACTS"
  }

  source {
    type      = "S3"
    location  = "${var.state_bucket}/codebuild/adp-source.zip"
    buildspec = each.value.buildspec
  }

  environment {
    type                        = "LINUX_CONTAINER"
    image                       = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    compute_type                = "BUILD_GENERAL1_MEDIUM"
    privileged_mode             = true
    image_pull_credentials_type = "CODEBUILD"
  }

  tags = var.common_tags
}
