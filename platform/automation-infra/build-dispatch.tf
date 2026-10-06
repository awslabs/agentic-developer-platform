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

# These capabilities belong to the independent automation state, never the
# domain installer or selected connected runtime role. Omitted flags add no grant.
variable "enable_superplane_operator_source" {
  description = "Allow the protected build publisher to deliver reviewed aws-e/adp Git history through the exact private Superplane source prefix."
  type        = bool
  default     = false
  nullable    = false
}

variable "enable_superplane_paid_release" {
  description = "Enroll only the prepared Superplane paid worker project, repository, source staging and durable dispatch evidence. Does not provision the builder."
  type        = bool
  default     = false
  nullable    = false
}

locals {
  superplane_build_enabled = var.enable_superplane_operator_source || var.enable_superplane_paid_release
  build_dispatch_projects = distinct(concat(var.build_project_names,
  var.enable_superplane_paid_release ? ["adp-${var.environment}-superplane-paid-worker"] : []))
  superplane_source_prefix = "arn:aws:s3:::adp-terraform-state-${data.aws_caller_identity.current.account_id}/superplane/releases/operator-source/${var.environment}"
  superplane_claim_prefix  = "arn:aws:s3:::adp-terraform-state-${data.aws_caller_identity.current.account_id}/superplane/releases/paid-worker/dispatch"

  build_layer_artifact_keys = [for project, key in {
    "adp-${var.environment}-pyjwt-layer"    = "lambda-layers/pyjwt-py313.zip"
    "adp-${var.environment}-psycopg2-layer" = "lambda-layers/psycopg2-py312.zip"
  } : key if contains(var.build_project_names, project)]
  build_ecr_read_names = var.build_ecr_repository_names == null ? ["adp-*"] : distinct(concat(
  var.build_ecr_repository_names, var.enable_superplane_paid_release ? ["adp-superplane-paid-worker"] : []))
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
  lifecycle {
    precondition {
      condition = !local.superplane_build_enabled || (
        var.repository == "aws-e/adp" && var.name_prefix == "adp-${var.environment}" &&
        can(regex("^[a-z0-9]+(-[a-z0-9]+)*$", var.environment))
      )
      error_message = "Superplane publication requires aws-e/adp, an exact environment and its canonical adp-<environment>-trusted-build identity."
    }
  }
  name = "reviewed-project-dispatch"
  role = aws_iam_role.build.id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    {
      Sid    = "DispatchKnownProjects", Effect = "Allow",
      Action = ["codebuild:StartBuild", "codebuild:StopBuild", "codebuild:BatchGetBuilds", "codebuild:BatchGetProjects"],
      Resource = flatten([for name in local.build_dispatch_projects : [
        "arn:aws:codebuild:${var.aws_region}:${data.aws_caller_identity.current.account_id}:project/${name}",
        "arn:aws:codebuild:${var.aws_region}:${data.aws_caller_identity.current.account_id}:build/${name}:*",
      ]])
    },
    {
      Sid      = "StageReviewedSource", Effect = "Allow", Action = ["s3:PutObject"],
      Resource = [for name in local.build_dispatch_projects : "arn:aws:s3:::adp-terraform-state-${data.aws_caller_identity.current.account_id}/codebuild/src/${name}/*"]
    },
    {
      Sid      = "ReadPublishedImages", Effect = "Allow",
      Action   = ["ecr:DescribeImages", "ecr:DescribeRepositories", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
      Resource = [for name in local.build_ecr_read_names : "arn:aws:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/${name}"]
    },
    {
      Sid = "ImageAuthentication", Effect = "Allow", Action = ["ecr:GetAuthorizationToken", "sts:GetCallerIdentity"], Resource = "*"
    },
    ], [for _ in range(var.enable_superplane_operator_source ? 1 : 0) : {
      Sid      = "SuperplaneReviewedSource", Effect = "Allow",
      Action   = ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"],
      Resource = [for kind in ["bundles", "consumers", "manifests"] : "${local.superplane_source_prefix}/*/${kind}/*"]
      }], [for _ in range(var.enable_superplane_operator_source ? 1 : 0) : {
      Sid      = "SuperplaneSourceBucketChecks", Effect = "Allow",
      Action   = ["s3:GetBucketVersioning", "s3:GetBucketPublicAccessBlock", "s3:GetBucketOwnershipControls", "s3:GetBucketLocation"],
      Resource = "arn:aws:s3:::adp-terraform-state-${data.aws_caller_identity.current.account_id}"
      }], [for _ in range(var.enable_superplane_paid_release ? 1 : 0) : {
      Sid      = "SuperplanePaidDispatchEvidence", Effect = "Allow", Action = ["s3:PutObject"],
      Resource = [for name in ["claim.json", "child.json"] : "${local.superplane_claim_prefix}/*/${name}"]
      }], [for _ in range(var.enable_superplane_paid_release ? 1 : 0) : {
      Sid      = "SuperplanePaidRetentionChecks", Effect = "Allow",
      Action   = ["s3:GetBucketVersioning", "s3:GetLifecycleConfiguration"],
      Resource = "arn:aws:s3:::adp-terraform-state-${data.aws_caller_identity.current.account_id}"
      }], [for _ in range(length(local.build_layer_artifact_keys) > 0 ? 1 : 0) : {
      Sid      = "VerifyPublishedLayerArtifacts", Effect = "Allow", Action = ["s3:GetObject"],
      Resource = [for key in local.build_layer_artifact_keys : "arn:aws:s3:::adp-terraform-state-${data.aws_caller_identity.current.account_id}/${key}"]
      }], [for _ in range(var.build_publish_worker_image_tag ? 1 : 0) : {
      Sid      = "PublishWorkerBuildTag", Effect = "Allow", Action = ["ssm:PutParameter"],
      Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/cyber/worker-image-tag"
    }
  ]) })
}
output "build_role_arn" { value = aws_iam_role.build.arn }
