# Publishing builds are admitted only from protected, reviewed main workflows.
# PR smoke builds use their own non-publishing project and never this identity.
variable "build_project_names" {
  description = "Exact reviewed project names from platform's codebuild_project_names output plus the agent-context image projects. Never prefixes."
  type        = list(string)
  validation {
    condition     = length(var.build_project_names) > 0 && alltrue([for name in var.build_project_names : can(regex("^adp-[A-Za-z0-9_-]+$", name))])
    error_message = "Supply a nonempty explicit build project inventory."
  }
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
  policy = jsonencode({ Version = "2012-10-17", Statement = [
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
      Resource = "arn:aws:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/adp-*"
    },
    {
      Sid = "ImageAuthentication", Effect = "Allow", Action = ["ecr:GetAuthorizationToken", "sts:GetCallerIdentity"], Resource = "*"
    },
    {
      Sid      = "PublishWorkerManifest", Effect = "Allow", Action = ["s3:PutObject"],
      Resource = "arn:aws:s3:::adp-${var.environment}-cape-assets/manifests/*"
    },
  ] })
}
output "build_role_arn" { value = aws_iam_role.build.arn }
