# =============================================================================
# CodeBuild Projects — docker-requiring builds only
# =============================================================================
# Only builds that need `privileged_mode = true` (i.e. a container runtime)
# belong here. Everything else (Terraform apply, npm build, kubectl apply) runs
# directly on the ARC runner.
#
# IAM model (A18, #5674): every project gets its OWN role whose policy names
# only the resources that project's buildspec actually touches. Before A18 all
# projects shared one role with `AdministratorAccess` attached, so the build of
# any one component could push a tampered image of every other component, read
# every object in the Terraform state bucket (i.e. every credential Terraform
# has ever recorded in state), and create itself an administrator role. The
# executed buildspec is read out of a zip in that same shared bucket, so that
# administrator capability was reachable by anyone who could write the zip.
#
# The per-project resource lists below were derived by reading each buildspec in
# codebuild/ and the scripts they call — not by copying the previous grant.
# =============================================================================

locals {
  # Per-project build contract. Every field is a permission decision:
  #
  #   buildspec     — the spec executed from the source zip.
  #   ecr_repos     — ECR repositories this project may push to. A project with
  #                   an empty list gets NO push permission at all.
  #   s3_write      — object keys (under the state bucket) this project may
  #                   write. Used by the two Lambda-layer builds, which publish
  #                   a zip that gateway Terraform then reads.
  #   scan_upload   — true for the two vulnerability/SBOM scanners, which write
  #                   findings to the security-scans bucket and read images to
  #                   scan them, but never push an image.
  #   privileged    — host-container privilege (Docker-in-Docker).
  #   privileged_why— why it is required. Enforced as an allow list: see the
  #                   `privileged` assertion in the tests. #5674 asked for this
  #                   flag to be dropped from every project that does not build
  #                   images; reading the buildspecs showed there is no such
  #                   project here. All ten invoke a container runtime,
  #                   INCLUDING the two Lambda-layer builds, which look like
  #                   plain zip packaging but compile their dependencies inside
  #                   the official Lambda runtime image for binary
  #                   compatibility (see modules/gateway/lambda/layers/*/
  #                   build.sh). Removing the flag from those would break them.
  #                   So the flag stays where it is genuinely needed and the
  #                   requirement becomes explicit and reviewable: a project
  #                   added to this map must state its reason, rather than
  #                   inheriting privilege silently from a shared default.
  build_api_ceiling = [
    "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
    "s3:GetObject", "s3:GetObjectVersion", "s3:PutObject",
    "ecr:GetAuthorizationToken", "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage",
    "ecr:CompleteLayerUpload", "ecr:CreateRepository", "ecr:DescribeImages", "ecr:DescribeRepositories",
    "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload", "ecr:ListImages", "ecr:PutImage", "ecr:UploadLayerPart",
    "kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey", "sts:GetCallerIdentity",
  ]
  core_projects = {
    "gateway-build" = {
      buildspec      = "codebuild/bs-gateway-build.yml"
      ecr_repos      = ["adp-gateway"]
      privileged     = true
      privileged_why = "docker build + docker run of the pricing/review/evaluation selfchecks before push"
    }
    "chat-agent" = {
      buildspec      = "codebuild/bs-chat-agent.yml"
      ecr_repos      = ["adp-chat-agent"]
      privileged     = true
      privileged_why = "docker build -f agent/Dockerfile"
    }
    "agent-gateway" = {
      buildspec      = "codebuild/bs-agent-gateway.yml"
      ecr_repos      = ["adp-agent-gateway"]
      privileged     = true
      privileged_why = "docker build -f gateway/Dockerfile"
    }
    "arc-runner" = {
      buildspec      = "codebuild/bs-arc-runner.yml"
      ecr_repos      = ["adp-arc-runner"]
      privileged     = true
      privileged_why = "docker build of the self-hosted runner image"
    }
    "agent-runtime" = {
      buildspec      = "codebuild/bs-agent-runtime.yml"
      ecr_repos      = ["adp-agent-runtime"]
      build_timeout  = 90
      compute_type   = "BUILD_GENERAL1_LARGE"
      privileged     = true
      privileged_why = "docker build + docker run of lib.contract_selfcheck before push"
    }
    "pyjwt-layer" = {
      buildspec      = "codebuild/bs-pyjwt-layer.yml"
      s3_write       = ["lambda-layers/pyjwt-py313.zip"]
      privileged     = true
      privileged_why = "layers/pyjwt/build.sh compiles PyJWT[crypto] inside the Lambda runtime image for binary compatibility"
    }
    "psycopg2-layer" = {
      buildspec      = "codebuild/bs-psycopg2-layer.yml"
      s3_write       = ["lambda-layers/psycopg2-py312.zip"]
      privileged     = true
      privileged_why = "layers/psycopg2/build.sh compiles psycopg2-binary inside the Lambda runtime image for binary compatibility"
    }
    "grype-scan" = {
      buildspec      = "codebuild/bs-grype-scan.yml"
      build_timeout  = 90
      scan_upload    = true
      privileged     = true
      privileged_why = "scan_security_images.py builds/pulls each scan target with docker before grype reads it"
    }
    "syft-scan" = {
      buildspec      = "codebuild/bs-syft-scan.yml"
      scan_upload    = true
      privileged     = true
      privileged_why = "scan_security_images.py builds/pulls each scan target with docker before syft reads it"
    }
    "superplane-api" = {
      buildspec      = "modules/domain-apps/superplane/releases/buildspecs/api.yml"
      ecr_repos      = ["adp-superplane-api"]
      privileged     = true
      privileged_why = "docker build of the maintained Superplane API image"
    }
    "superplane-controller" = {
      buildspec      = "modules/domain-apps/superplane/releases/buildspecs/controller.yml"
      ecr_repos      = ["adp-superplane-controller"]
      privileged     = true
      privileged_why = "docker build of the maintained Superplane controller image"
    }
    "superplane-monitor" = {
      buildspec      = "modules/domain-apps/superplane/releases/buildspecs/monitor.yml"
      ecr_repos      = ["adp-superplane-platform-monitor"]
      privileged     = true
      privileged_why = "docker build of the maintained Superplane platform monitor image"
    }
    "superplane-executor" = {
      buildspec      = "modules/domain-apps/superplane/releases/buildspecs/executor.yml"
      ecr_repos      = ["adp-superplane-executor"]
      privileged     = true
      privileged_why = "docker build of the maintained Superplane executor image"
    }
  }

  projects = merge(local.core_projects, [for manifest in sort(tolist(fileset("${path.module}/../../../../modules/domain-apps", "*/codebuild/projects.json"))) :
    jsondecode(file("${path.module}/../../../../modules/domain-apps/${manifest}"))
  ]...)

  agent_context_images = toset([
    "ingestion",
    "codegraph-context",
    "litellm-proxy",
    "deepwiki",
    "context-mcp",
  ])

  ecr_repo_arn_prefix = "arn:aws:ecr:${var.aws_region}:${var.account_id}:repository"
}

# -----------------------------------------------------------------------------
# Permissions boundary for every build role
# -----------------------------------------------------------------------------
# The per-project policies below grant no identity permissions, but a policy is
# data that a later edit can widen. A boundary caps what ANY policy attached to
# these roles can achieve, so re-adding iam:CreateRole to a project policy
# cannot revive the capability. That is the difference between removing today's
# escalation path and removing the class of escalation path.
#
# The boundary opens with `Allow "*"` and then subtracts. That is deliberate and
# it is NOT the same shape as the broad grant this issue removes elsewhere:
#
#   A permissions boundary is an upper bound, not a grant. An action is permitted
#   only when the identity policy allows it AND the boundary allows it. So a
#   boundary containing only Deny statements permits NOTHING — attaching one
#   would fail every build on the next apply, which is precisely the
#   "over-tightened permissions break automation at the worst moment" failure
#   this issue's impact analysis warns about, and the pressure that gets a broad
#   grant reattached as a hotfix.
#
#   The grant of record is therefore the per-project policy below; the boundary's
#   job is to make the escalation classes unreachable no matter what any policy
#   on these roles says. Keeping the Allow at "*" means it never has to be
#   widened when a project legitimately gains a resource, so the Deny list stays
#   the only thing anyone edits here — unlike the runner boundary's
#   "AllowBroadAccess", which enumerated services AND the escalation actions and
#   so permitted the escalation it was meant to prevent.
# -----------------------------------------------------------------------------
resource "aws_iam_policy" "codebuild_boundary" {
  name        = "${var.name_prefix}-codebuild-boundary"
  description = "Permissions boundary for ADP CodeBuild project roles — caps them below privilege escalation, role assumption and build-input tampering"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # The upper bound. Effective permissions are this intersected with each
        # project's own policy, minus every Deny below.
        Sid      = "BoundaryCeiling"
        Effect   = "Allow"
        Action   = local.build_api_ceiling
        Resource = "*"
      },
      {
        Sid       = "DenyOutsideBuildAPIs"
        Effect    = "Deny"
        NotAction = local.build_api_ceiling
        Resource  = "*"
      },
      {
        # Without this the build can create a role, attach AdministratorAccess
        # to it, and assume it — so it does not need admin to obtain admin.
        # iam:PassRole is included because handing an existing privileged role
        # to a service the build can invoke reaches the same outcome without
        # creating anything.
        Sid    = "DenyIdentityMutation"
        Effect = "Deny"
        Action = [
          "iam:AddClientIDToOpenIDConnectProvider",
          "iam:AddRoleToInstanceProfile",
          "iam:AddUserToGroup",
          "iam:AttachGroupPolicy",
          "iam:AttachRolePolicy",
          "iam:AttachUserPolicy",
          "iam:CreateAccessKey",
          "iam:CreateGroup",
          "iam:CreateInstanceProfile",
          "iam:CreateLoginProfile",
          "iam:CreateOpenIDConnectProvider",
          "iam:CreatePolicy",
          "iam:CreatePolicyVersion",
          "iam:CreateRole",
          "iam:CreateSAMLProvider",
          "iam:CreateServiceLinkedRole",
          "iam:CreateUser",
          "iam:DeletePolicy",
          "iam:DeletePolicyVersion",
          "iam:DeleteRole",
          "iam:DeleteRolePermissionsBoundary",
          "iam:DeleteRolePolicy",
          "iam:DeleteUser",
          "iam:DeleteUserPermissionsBoundary",
          "iam:DeleteUserPolicy",
          "iam:DetachGroupPolicy",
          "iam:DetachRolePolicy",
          "iam:DetachUserPolicy",
          "iam:PassRole",
          "iam:PutGroupPolicy",
          "iam:PutRolePermissionsBoundary",
          "iam:PutRolePolicy",
          "iam:PutUserPermissionsBoundary",
          "iam:PutUserPolicy",
          "iam:SetDefaultPolicyVersion",
          "iam:UpdateAssumeRolePolicy",
          "iam:UpdateLoginProfile",
          "iam:UpdateRole",
          "iam:UpdateUser",
          "sts:AssumeRole",
          "sts:AssumeRoleWithSAML",
          "sts:AssumeRoleWithWebIdentity"
        ]
        Resource = "*"
      },
      {
        # A build that can rewrite the source zip or a buildspec in the state
        # bucket chooses what the NEXT build executes, which is the supply-chain
        # path this issue exists to close. Reads of codebuild/* are granted
        # per project below; writes there are denied to all of them.
        Sid    = "DenyBuildInputTampering"
        Effect = "Deny"
        Action = [
          "s3:DeleteObject",
          "s3:DeleteObjectVersion",
          "s3:PutObject",
          "s3:PutObjectAcl"
        ]
        Resource = "arn:aws:s3:::${var.state_bucket}/codebuild/*"
      },
      {
        # Terraform state in this bucket records generated passwords, tokens and
        # connection strings in plaintext. No build has a reason to read it.
        Sid    = "DenyTerraformStateAccess"
        Effect = "Deny"
        Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject"]
        Resource = [
          "arn:aws:s3:::${var.state_bucket}/*.tfstate",
          "arn:aws:s3:::${var.state_bucket}/*.tfstate.*",
          "arn:aws:s3:::${var.state_bucket}/env:/*",
          "arn:aws:s3:::${var.state_bucket}/*/terraform.tfstate"
        ]
      },
      {
        # Reading the secret store is how a build would collect other tenants'
        # credentials. No buildspec in codebuild/ reads a secret; none should
        # start doing so without this boundary being revisited.
        Sid    = "DenySecretAndParameterReads"
        Effect = "Deny"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:ListSecrets",
          "secretsmanager:PutSecretValue",
          "ssm:GetParameter",
          "ssm:GetParameters",
          "ssm:GetParametersByPath",
          "ssm:PutParameter"
        ]
        Resource = "*"
      },
      {
        Sid    = "DenyAccountAndOrgControl"
        Effect = "Deny"
        Action = [
          "account:*",
          "aws-portal:*",
          "billing:*",
          "ce:*",
          "organizations:*"
        ]
        Resource = "*"
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# Per-project IAM roles
# -----------------------------------------------------------------------------
data "aws_iam_policy_document" "codebuild_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
    # Confused-deputy protection: without this, any CodeBuild project in any
    # account that AWS asks to assume this role would be accepted.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "project" {
  for_each = local.projects

  name        = "${var.name_prefix}-codebuild-${each.key}"
  description = "Build role for ${var.name_prefix}-${each.key} — scoped to that project's own resources (A18, #5674)"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Principal = { Service = "codebuild.amazonaws.com" }, Action = "sts:AssumeRole",
    Condition = { StringEquals = { "aws:SourceAccount" = var.account_id, "aws:SourceArn" = "arn:aws:codebuild:${var.aws_region}:${var.account_id}:project/${var.name_prefix}-${each.key}" } }
  }] })
  permissions_boundary = aws_iam_policy.codebuild_boundary.arn
  tags                 = var.common_tags
}

resource "aws_iam_role_policy" "project" {
  for_each = local.projects

  name = "build-scope"
  role = aws_iam_role.project[each.key].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          # CodeBuild writes the build log here. Scoped to this project's own
          # log group so one build cannot rewrite another's audit trail.
          Sid    = "OwnBuildLogs"
          Effect = "Allow"
          Action = [
            "logs:CreateLogGroup",
            "logs:CreateLogStream",
            "logs:PutLogEvents"
          ]
          Resource = [
            "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/codebuild/${var.name_prefix}-${each.key}",
            "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/codebuild/${var.name_prefix}-${each.key}:*"
          ]
        },
        {
          # The source zip CodeBuild downloads before the build starts. Read
          # only, and only under codebuild/ — not the whole state bucket.
          Sid      = "BuildSourceRead"
          Effect   = "Allow"
          Action   = ["s3:GetObject", "s3:GetObjectVersion"]
          Resource = "arn:aws:s3:::${var.state_bucket}/codebuild/src/${var.name_prefix}-${each.key}/*"
        }
      ],
      # Each optional block below is generated by iterating a 0- or 1-element
      # range rather than by a `cond ? [...] : []` ternary. Terraform requires
      # both arms of a ternary to have the same type, and a populated tuple of
      # policy statements never unifies with an empty one — so the ternary form
      # fails to validate as soon as the statements differ in shape.
      #
      # ECR registry auth. Resource must be "*": GetAuthorizationToken is a
      # registry-level call with no repository ARN to scope it to. It yields a
      # token whose reach is decided by the repository grants below.
      [for _ in range((length(lookup(each.value, "ecr_repos", [])) > 0 || lookup(each.value, "scan_upload", false)) ? 1 : 0) : {
        Sid      = "EcrAuth"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      }],
      # Push, only to this project's own repositories.
      [for _ in range(length(lookup(each.value, "ecr_repos", [])) > 0 ? 1 : 0) : {
        Sid    = "OwnEcrRepositories"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:CompleteLayerUpload",
          "ecr:DescribeImages",
          "ecr:DescribeRepositories",
          "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload",
          "ecr:ListImages",
          "ecr:PutImage",
          "ecr:UploadLayerPart"
        ]
        Resource = [for repo in each.value.ecr_repos : "${local.ecr_repo_arn_prefix}/${repo}"]
      }],
      # The buildspecs retain a first-run-safe CreateRepository call. IAM can
      # authorize that call against the exact repository ARN even before the
      # repository exists, so no prefix-wide bootstrap grant is needed.
      [for _ in range(length(lookup(each.value, "ecr_repos", [])) > 0 ? 1 : 0) : {
        Sid      = "EcrRepositoryBootstrap"
        Effect   = "Allow"
        Action   = ["ecr:CreateRepository"]
        Resource = [for repo in each.value.ecr_repos : "${local.ecr_repo_arn_prefix}/${repo}"]
      }],
      # Lambda-layer publication: the exact object key gateway Terraform reads.
      [for _ in range(length(lookup(each.value, "s3_write", [])) > 0 ? 1 : 0) : {
        Sid      = "LayerArtifactPublish"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = [for key in each.value.s3_write : "arn:aws:s3:::${var.state_bucket}/${key}"]
      }],
      # App-owned build descriptors declare their artifact publication paths.
      [for output in lookup(each.value, "artifact_writes", []) : {
        Sid      = "AppArtifactPublish${substr(sha256(output.prefix), 0, 12)}"
        Effect   = "Allow", Action = ["s3:PutObject"],
        Resource = "arn:aws:s3:::${var.name_prefix}-${output.bucket_suffix}/${output.prefix}/*"
      }],
      # Scanners write findings to the security-scans bucket.
      [for _ in range(lookup(each.value, "scan_upload", false) ? 1 : 0) : {
        Sid    = "ScanFindingsUpload"
        Effect = "Allow"
        Action = ["s3:PutObject"]
        Resource = [
          "${var.security_scans_bucket_arn}/sarif/*",
          "${var.security_scans_bucket_arn}/sbom/*",
          "${var.security_scans_bucket_arn}/repository-scans/*"
        ]
      }],
      # Pull-only. A scanner reads images; it must never be able to replace one
      # with a "scanned clean" build of its own.
      [for _ in range(lookup(each.value, "scan_upload", false) ? 1 : 0) : {
        Sid    = "ScanImageRead"
        Effect = "Allow"
        Action = [
          "ecr:BatchGetImage",
          "ecr:DescribeImages",
          "ecr:DescribeRepositories",
          "ecr:GetDownloadUrlForLayer",
          "ecr:ListImages"
        ]
        Resource = "${local.ecr_repo_arn_prefix}/*"
      }],
      # bs-grype-scan.yml / bs-syft-scan.yml call this in pre_build as a
      # credential smoke test. It takes no resource and reveals nothing the
      # caller does not already know.
      [for _ in range(lookup(each.value, "scan_upload", false) ? 1 : 0) : {
        Sid      = "CallerIdentity"
        Effect   = "Allow"
        Action   = ["sts:GetCallerIdentity"]
        Resource = "*"
      }]
    )
  })
}

# PR validation reuses gateway-build with a restricted service-role override.
# The ordinary runner must explicitly select this role; its runtime policy
# denies StartBuild when the role override is absent or names any other role.
# Publishing keeps the existing project role and source prefix unchanged.
resource "aws_iam_role" "gateway_pr" {
  name                 = "${var.name_prefix}-codebuild-gateway-pr"
  description          = "Nonpublishing PR validation on the existing gateway-build project"
  permissions_boundary = aws_iam_policy.codebuild_boundary.arn
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "codebuild.amazonaws.com" }, Action = "sts:AssumeRole",
    Condition = { StringEquals = {
      "aws:SourceAccount" = var.account_id,
      "aws:SourceArn"     = "arn:aws:codebuild:${var.aws_region}:${var.account_id}:project/${var.name_prefix}-gateway-build"
    } }
  }] })
  tags = var.common_tags
}

resource "aws_iam_role_policy" "gateway_pr" {
  name = "pr-validation-only"
  role = aws_iam_role.gateway_pr.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Sid    = "OwnBuildLogs", Effect = "Allow",
      Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      Resource = [
        "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/codebuild/${var.name_prefix}-gateway-build",
        "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/codebuild/${var.name_prefix}-gateway-build:*"
      ]
    },
    {
      Sid      = "PrSourceRead", Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"],
      Resource = "arn:aws:s3:::${var.state_bucket}/codebuild/src/${var.name_prefix}-gateway-build-pr/*"
    },
    {
      # Also constrain resource-policy grants made directly to a role session.
      Sid       = "DenyOtherApis", Effect = "Deny",
      NotAction = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "s3:GetObject", "s3:GetObjectVersion"],
      Resource  = "*"
    },
    {
      Sid         = "DenyOtherSource", Effect = "Deny", Action = ["s3:GetObject", "s3:GetObjectVersion"],
      NotResource = "arn:aws:s3:::${var.state_bucket}/codebuild/src/${var.name_prefix}-gateway-build-pr/*"
    }
  ] })
}

# -----------------------------------------------------------------------------
# Per-project build roles for agent-context images
# -----------------------------------------------------------------------------
# The projects live in the agent-context state, while their IAM ceiling lives in
# platform state. The output contract is therefore a map keyed by image. Each
# role can write one exact repository and one exact log group; no image build can
# replace a sibling project's artifact.
resource "aws_iam_role" "agent_context_image" {
  for_each = local.agent_context_images

  name        = "${var.name_prefix}-codebuild-agent-context-${each.key}"
  description = "Build role for the ${each.key} agent-context image (A18, #5674)"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Principal = { Service = "codebuild.amazonaws.com" }, Action = "sts:AssumeRole",
    Condition = { StringEquals = { "aws:SourceAccount" = var.account_id, "aws:SourceArn" = "arn:aws:codebuild:${var.aws_region}:${var.account_id}:project/${var.name_prefix}-agent-context-${each.key}-build" } }
  }] })
  permissions_boundary = aws_iam_policy.codebuild_boundary.arn
  tags                 = var.common_tags
}

resource "aws_iam_role_policy" "agent_context_image" {
  for_each = local.agent_context_images

  name = "build-scope"
  role = aws_iam_role.agent_context_image[each.key].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "OwnBuildLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = [
          "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/codebuild/${var.name_prefix}-agent-context-${each.key}-build",
          "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/codebuild/${var.name_prefix}-agent-context-${each.key}-build:*"
        ]
      },
      {
        Sid      = "BuildSourceRead"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = "arn:aws:s3:::${var.state_bucket}/codebuild/src/${var.name_prefix}-agent-context-${each.key}-build/*"
      },
      {
        Sid      = "EcrAuth"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid    = "OwnEcrRepository"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:CompleteLayerUpload",
          "ecr:DescribeImages",
          "ecr:DescribeRepositories",
          "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload",
          "ecr:ListImages",
          "ecr:PutImage",
          "ecr:UploadLayerPart"
        ]
        Resource = "${local.ecr_repo_arn_prefix}/${var.name_prefix}-agent-context-${each.key}"
      },
      {
        Sid    = "EcrEncryptionKeyUse"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
          "kms:GenerateDataKey"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ecr.${var.aws_region}.amazonaws.com"
          }
        }
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# CodeBuild projects (one per docker-build workflow)
# -----------------------------------------------------------------------------
resource "aws_codebuild_project" "main" {
  for_each = local.projects

  name          = "${var.name_prefix}-${each.key}"
  description   = "ADP docker build: ${each.key}"
  service_role  = aws_iam_role.project[each.key].arn
  build_timeout = lookup(each.value, "build_timeout", 60)

  artifacts {
    type = "NO_ARTIFACTS"
  }

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
      value = var.ecr_registry
    }
  }

  logs_config {
    cloudwatch_logs {
      group_name  = "/aws/codebuild/${var.name_prefix}-${each.key}"
      stream_name = ""
    }
  }

  tags = var.common_tags
}

# -----------------------------------------------------------------------------
# S3 lifecycle rule — expire per-build source artifacts after 7 days
# -----------------------------------------------------------------------------
# Per-build source zips are uploaded to codebuild/src/<sha>-<run_id>.zip by
# workflows and scripts. This lifecycle rule prevents unbounded storage growth.
# The state bucket is bootstrap-managed (not TF-managed), so we reference it
# as a data source to attach the lifecycle configuration.
# -----------------------------------------------------------------------------

data "aws_s3_bucket" "state" {
  bucket = var.state_bucket
}

resource "aws_s3_bucket_lifecycle_configuration" "codebuild_source_expiry" {
  bucket = data.aws_s3_bucket.state.id

  rule {
    id     = "expire-codebuild-source-artifacts"
    status = "Enabled"

    filter {
      prefix = "codebuild/src/"
    }

    expiration {
      days = 7
    }
  }
}
