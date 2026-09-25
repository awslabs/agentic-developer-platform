# Publishing builds are admitted only from protected, reviewed main workflows.
# PR smoke builds reuse gateway-build with a restricted service-role override
# and separate source prefix, never this identity.
variable "build_project_names" {
  description = "Exact reviewed project names from platform's codebuild_project_names output plus the agent-context image projects. Never prefixes."
  type        = list(string)
  validation {
    condition     = length(var.build_project_names) > 0 && alltrue([for name in var.build_project_names : can(regex("^adp-[A-Za-z0-9_-]+$", name))])
    error_message = "Supply a nonempty explicit build project inventory."
  }
}

# Null preserves the existing multi-image dispatcher. New narrowly scoped
# installations should always pass exact repository names.
variable "build_ecr_repository_names" {
  description = "Exact ECR repositories the dispatcher may inspect/pull. Null retains the existing adp-* read scope; explicit lists cannot contain wildcards."
  type        = list(string)
  default     = null
  validation {
    condition = var.build_ecr_repository_names == null ? true : (
      length(var.build_ecr_repository_names) > 0 && alltrue([
        for name in var.build_ecr_repository_names : can(regex("^adp-[a-z0-9][a-z0-9._/-]*$", name))
      ])
    )
    error_message = "Supply a nonempty list of exact adp- ECR repository names, without ARNs or wildcards."
  }
}

variable "build_publish_worker_image_tag" {
  description = "Retain the existing Cyber worker-image-tag publication capability. Disable for dispatchers that do not publish that pointer."
  type        = bool
  default     = true
  nullable    = false
}

locals {
  build_ecr_read_names = var.build_ecr_repository_names == null ? ["adp-*"] : var.build_ecr_repository_names
}

resource "aws_iam_role" "build" {
  name = "${var.name_prefix}-trusted-build"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:${var.repository}:environment:adp-build-${var.environment}"
    } }
  }] })
}

resource "aws_iam_role_policy" "build_dispatch" {
  name = "reviewed-project-dispatch"
  role = aws_iam_role.build.id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    {
      Sid    = "DispatchKnownProjects", Effect = "Allow",
      Action = ["codebuild:StartBuild", "codebuild:StopBuild", "codebuild:BatchGetBuilds", "codebuild:BatchGetProjects"],
      Resource = flatten([for name in var.build_project_names : [
        "arn:aws:codebuild:${var.aws_region}:${data.aws_caller_identity.current.account_id}:project/${name}",
        "arn:aws:codebuild:${var.aws_region}:${data.aws_caller_identity.current.account_id}:build/${name}:*",
      ]])
    },
    {
      Sid      = "StageReviewedSource", Effect = "Allow", Action = ["s3:PutObject"],
      Resource = [for name in var.build_project_names : "arn:aws:s3:::adp-terraform-state-${data.aws_caller_identity.current.account_id}/codebuild/src/${name}/*"]
    },
    {
      Sid      = "ReadPublishedImages", Effect = "Allow",
      Action   = ["ecr:DescribeImages", "ecr:DescribeRepositories", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
      Resource = [for name in local.build_ecr_read_names : "arn:aws:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/${name}"]
    },
    {
      Sid = "ImageAuthentication", Effect = "Allow", Action = ["ecr:GetAuthorizationToken", "sts:GetCallerIdentity"], Resource = "*"
    },
    ], [for _ in range(var.build_publish_worker_image_tag ? 1 : 0) : {
      Sid      = "PublishWorkerBuildTag", Effect = "Allow", Action = ["ssm:PutParameter"],
      Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/cyber/worker-image-tag"
    }
  ]) })
}
output "build_role_arn" { value = aws_iam_role.build.arn }
