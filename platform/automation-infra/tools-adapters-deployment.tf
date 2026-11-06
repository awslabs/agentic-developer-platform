# Publication authority for the separately deployed adapter image. Bootstrap
# provisions the repository/project; the protected tools release role only uses
# them. No IAM provisioning, additional PassRole, provider access or state writes.
variable "enable_tools_adapters_publication" {
  type        = bool
  default     = false
  description = "Enable publication to the already provisioned adapter repository and qualifier for this tools release environment."
  validation {
    condition     = !var.enable_tools_adapters_publication || (var.enable_tools_deployment && contains(["dev", "prod"], var.environment))
    error_message = "Adapter publication requires the protected tools deployment identity in dev or prod."
  }
}

locals {
  tools_adapters_enabled          = var.enable_tools_deployment && var.enable_tools_adapters_publication
  tools_adapters_name             = "adp-tools-${var.environment}-adapters"
  tools_adapters_repository       = "arn:aws:ecr:${var.aws_region}:${local.tools_account}:repository/${local.tools_adapters_name}"
  tools_adapters_project          = "arn:aws:codebuild:${var.aws_region}:${local.tools_account}:project/${local.tools_adapters_name}-build"
  tools_adapters_source           = "${local.tools_state_bucket}/codebuild/src/${local.tools_adapters_name}-build/*"
  tools_adapters_archive          = "${local.tools_state_bucket}/codebuild/artifacts/${local.tools_adapters_name}-build/*"
  tools_adapters_log_group        = "arn:aws:logs:${var.aws_region}:${local.tools_account}:log-group:/aws/codebuild/${local.tools_adapters_name}-build:*"
  tools_adapters_check_name       = "${local.tools_adapters_name}-foundation-check"
  tools_adapters_check_project    = "arn:aws:codebuild:${var.aws_region}:${local.tools_account}:project/${local.tools_adapters_check_name}"
  tools_adapters_check_source     = "${local.tools_state_bucket}/codebuild/src/${local.tools_adapters_check_name}/*"
  tools_adapters_check_archive    = "${local.tools_state_bucket}/codebuild/artifacts/${local.tools_adapters_check_name}/*"
  tools_adapters_check_log        = "arn:aws:logs:${var.aws_region}:${local.tools_account}:log-group:/aws/codebuild/${local.tools_adapters_check_name}:*"
  tools_adapters_projects         = [local.tools_adapters_project, local.tools_adapters_check_project]
  tools_adapters_sources          = [local.tools_adapters_source, local.tools_adapters_check_source]
  tools_adapters_archive_versions = concat(local.tools_adapters_sources, [local.tools_adapters_archive, local.tools_adapters_check_archive])
  tools_adapters_repository_actions = [
    "ecr:DescribeRepositories", "ecr:DescribeImages", "ecr:BatchGetImage",
    "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
    "ecr:CompleteLayerUpload", "ecr:PutImage",
  ]
  # AWS authorizes these against the project ARN, including StopBuild and
  # BatchGetBuilds even though their API requests contain a build ID.
  # https://docs.aws.amazon.com/service-authorization/latest/reference/list_codebuild.html
  tools_adapters_build_actions = ["codebuild:StartBuild", "codebuild:BatchGetProjects", "codebuild:BatchGetBuilds", "codebuild:StopBuild"]
  tools_adapters_actions = local.tools_adapters_enabled ? concat(
    local.tools_adapters_repository_actions, local.tools_adapters_build_actions,
    ["s3:PutObject", "s3:GetObjectVersion", "logs:FilterLogEvents"],
  ) : []
  tools_adapters_policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "AdapterRepository", Effect = "Allow", Action = local.tools_adapters_repository_actions, Resource = local.tools_adapters_repository },
    { Sid = "AdapterBuild", Effect = "Allow", Action = local.tools_adapters_build_actions, Resource = local.tools_adapters_project },
    { Sid = "AdapterSource", Effect = "Allow", Action = "s3:PutObject", Resource = local.tools_adapters_source },
    # HeadObject/GetObject with version-id require GetObjectVersion. Reading the
    # current unversioned object is not part of the authenticated archive path.
    { Sid = "AdapterArchiveVersions", Effect = "Allow", Action = "s3:GetObjectVersion", Resource = [local.tools_adapters_source, local.tools_adapters_archive] },
    { Sid = "AdapterBuildLog", Effect = "Allow", Action = "logs:FilterLogEvents", Resource = local.tools_adapters_log_group },
    # Fence new actions even against independent resource-policy grants. Attach
    # these before opening either existing action ceiling during migration.
    { Sid = "DenyOtherAdapterBuildStops", Effect = "Deny", Action = "codebuild:StopBuild", NotResource = local.tools_adapters_projects },
    # Move this exact shared build fence out of the near-full resource policy
    # when publication is enabled. Its attachment precedes that policy update,
    # so both the existing facade and adapter stay bounded during migration.
    { Sid = "DenyOtherBuilds", Effect = "Deny", Action = ["codebuild:StartBuild", "codebuild:BatchGetProjects", "codebuild:BatchGetBuilds"], NotResource = concat([local.tools_build_project, local.tools_build], local.tools_adapters_projects) },
    { Sid = "DenyOtherArchiveVersions", Effect = "Deny", Action = "s3:GetObjectVersion", NotResource = local.tools_adapters_archive_versions },
    { Sid = "DenyOtherAdapterLogs", Effect = "Deny", Action = "logs:FilterLogEvents", NotResource = [local.tools_adapters_log_group, local.tools_adapters_check_log] },
    # Existing S3 ceilings already deny artifact writes and all object-version
    # deletions. Opening source upload must not also open its unversioned reads
    # or deletion through a bucket policy.
    { Sid = "DenyAdapterSourceReadDelete", Effect = "Deny", Action = ["s3:GetObject", "s3:DeleteObject"], Resource = local.tools_adapters_sources },
    { Sid = "DenyAdapterLayerDownload", Effect = "Deny", Action = "ecr:GetDownloadUrlForLayer", Resource = local.tools_adapters_repository },
  ] })
}

resource "aws_iam_policy" "tools_adapters_publication" {
  count      = local.tools_adapters_enabled ? 1 : 0
  name       = "${var.name_prefix}-tools-adapters-publication"
  policy     = local.tools_adapters_policy
  depends_on = [aws_iam_role_policy_attachment.tools_adapters_foundation_check]
  lifecycle {
    precondition {
      condition     = length(local.tools_adapters_policy) <= 6144
      error_message = "Adapter publication permissions exceed the managed-policy quota."
    }
  }
}

# The protected publisher dispatches checks and retrieves versioned evidence;
# only the separate CodeBuild role downloads images. No publisher role chaining,
# extra PassRole, production database access or test-runner deployment rights.
resource "aws_iam_policy" "tools_adapters_foundation_check" {
  count = local.tools_adapters_enabled ? 1 : 0
  name  = "${var.name_prefix}-tools-adapters-foundation-check"
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "FoundationCheckBuild", Effect = "Allow", Action = local.tools_adapters_build_actions, Resource = local.tools_adapters_check_project },
    { Sid = "FoundationCheckSource", Effect = "Allow", Action = "s3:PutObject", Resource = local.tools_adapters_check_source },
    { Sid = "FoundationCheckVersions", Effect = "Allow", Action = "s3:GetObjectVersion", Resource = [local.tools_adapters_check_source, local.tools_adapters_check_archive] },
    { Sid = "FoundationCheckLog", Effect = "Allow", Action = "logs:FilterLogEvents", Resource = local.tools_adapters_check_log },
  ] })
}

resource "aws_iam_role_policy_attachment" "tools_adapters_foundation_check" {
  count      = local.tools_adapters_enabled ? 1 : 0
  role       = aws_iam_role.tools_deployment[0].name
  policy_arn = aws_iam_policy.tools_adapters_foundation_check[0].arn
}

resource "aws_iam_role_policy_attachment" "tools_adapters_publication" {
  count      = local.tools_adapters_enabled ? 1 : 0
  role       = aws_iam_role.tools_deployment[0].name
  policy_arn = aws_iam_policy.tools_adapters_publication[0].arn
}
