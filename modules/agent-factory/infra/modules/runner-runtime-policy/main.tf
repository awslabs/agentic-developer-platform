# The shared runner executes repository input. Deployment is a different
# identity. Use the same effective ceiling in active and legacy installations.
variable "account_id" { type = string }
variable "aws_region" { type = string }
variable "name_prefix" { type = string }
variable "transport_secret_arns" {
  type        = list(string)
  default     = []
  description = "Reviewed exact legacy engine transport secrets, retained only until its separately authorized migration. Never tenant vaults, wildcard prefixes or deployment credentials."
  validation {
    condition = alltrue([for arn in var.transport_secret_arns :
      can(regex("^arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:(github-runner/[^/]+/[^/]+|adp/[^/]+/gh-app-(dev|pm|ops)-(id|key))-[A-Za-z0-9]{6}$", arn)) &&
      !can(regex("[*?]", arn))
    ])
    error_message = "Transport exceptions must be exact existing GitHub runner/app secret ARNs; broader secret inputs are forbidden."
  }
}

locals {
  gateway_pr_role = "arn:aws:iam::${var.account_id}:role/${var.name_prefix}-codebuild-gateway-pr"
  capabilities = {
    StartSmokeBuild = {
      actions   = ["codebuild:StartBuild"]
      resources = ["arn:aws:codebuild:${var.aws_region}:${var.account_id}:project/${var.name_prefix}-gateway-build"]
      condition = { StringEquals = { "codebuild:serviceRole" = local.gateway_pr_role } }
    }
    PassSmokeRole = {
      actions   = ["iam:PassRole"]
      resources = [local.gateway_pr_role]
      condition = { StringEquals = { "iam:PassedToService" = "codebuild.amazonaws.com" } }
    }
    SafeSmokeBuild = {
      actions = ["codebuild:BatchGetBuilds", "codebuild:BatchGetProjects", "codebuild:StopBuild"]
      resources = [
        "arn:aws:codebuild:${var.aws_region}:${var.account_id}:project/${var.name_prefix}-gateway-build",
        "arn:aws:codebuild:${var.aws_region}:${var.account_id}:build/${var.name_prefix}-gateway-build:*",
      ]
    }
    OwnSmokeSource = {
      actions = ["s3:PutObject", "s3:GetObject"]
      resources = [
        "arn:aws:s3:::adp-terraform-state-${var.account_id}/codebuild/src/${var.name_prefix}-gateway-build-pr/*",
        "arn:aws:s3:::${var.name_prefix}-security-scans-${var.account_id}/security-agent/*",
      ]
    }
    ScanLedgerList = {
      actions   = ["s3:ListBucket"]
      resources = ["arn:aws:s3:::${var.name_prefix}-security-scans-${var.account_id}"]
      condition = { StringLike = { "s3:prefix" = ["security-agent/*"] } }
    }
    Identity = {
      actions   = ["sts:GetCallerIdentity"]
      resources = ["*"]
    }
    ModelInference = {
      actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      resources = [
        "arn:aws:bedrock:*:${var.account_id}:inference-profile/*",
        "arn:aws:bedrock:*::foundation-model/anthropic.*",
      ]
    }
    GatewayEndpoint = {
      actions   = ["ssm:GetParameter"]
      resources = ["arn:aws:ssm:${var.aws_region}:${var.account_id}:parameter/adp/${trimprefix(var.name_prefix, "adp-")}/gateway/apigw-invoke-url"]
    }
    GatewayTransport = {
      actions = ["execute-api:Invoke"]
      resources = [
        "arn:aws:execute-api:${var.aws_region}:${var.account_id}:*/*/*/agent/*",
        "arn:aws:execute-api:${var.aws_region}:${var.account_id}:*/*/*/internal/*",
      ]
    }
    ImagePull = {
      actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
      resources = ["arn:aws:ecr:${var.aws_region}:${var.account_id}:repository/adp-*"]
    }
    ImageAuthentication = {
      actions   = ["ecr:GetAuthorizationToken"]
      resources = ["*"]
    }
    OwnLogs = {
      actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      resources = ["arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/adp/runner/${var.name_prefix}:*"]
    }
  }
  transport = length(var.transport_secret_arns) == 0 ? {} : {
    LegacyEngineTransport = {
      actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
      resources = var.transport_secret_arns
    }
  }
  allowed = merge(local.capabilities, local.transport)
  grants = [for name, capability in local.allowed : merge({
    Sid = name, Effect = "Allow", Action = capability.actions, Resource = capability.resources
  }, try({ Condition = capability.condition }, {}))]
  # Explicit denies also cover resource policies granting directly to a session.
  # The ceiling allows only the enumerated APIs. Resource and condition denies
  # below retain every scope without duplicating all the grants in this limited
  # 6144-character managed policy. IAM still intersects it with the grants.
  boundary = concat([
    {
      Sid    = "RuntimeApiCeiling", Effect = "Allow",
      Action = distinct(flatten([for capability in local.allowed : capability.actions])), Resource = "*"
    },
    {
      # A missing override must not inherit the project's publishing role.
      # AWS StartBuild exposes serviceRoleOverride as codebuild:serviceRole.
      Sid       = "DenyOtherBuildRole", Effect = "Deny", Action = ["codebuild:StartBuild"], Resource = "*",
      Condition = { StringNotEquals = { "codebuild:serviceRole" = local.gateway_pr_role } }
    },
    {
      Sid       = "DenyOtherPassService", Effect = "Deny", Action = ["iam:PassRole"], Resource = "*",
      Condition = { StringNotEquals = { "iam:PassedToService" = "codebuild.amazonaws.com" } }
    },
    {
      Sid       = "DenyOutsideRuntimeActions", Effect = "Deny",
      NotAction = flatten([for capability in local.allowed : capability.actions]), Resource = "*"
    },
    {
      Sid       = "DenyOtherLedgerPrefixes", Effect = "Deny", Action = ["s3:ListBucket"],
      Resource  = "arn:aws:s3:::${var.name_prefix}-security-scans-${var.account_id}",
      Condition = { StringNotLike = { "s3:prefix" = ["security-agent/*"] } }
    }
    ], [for name, capability in local.allowed : {
      Sid    = "DenyOther${name}Resources", Effect = "Deny",
      Action = capability.actions, NotResource = capability.resources
  } if capability.resources != ["*"]])
}

output "grants" { value = local.grants }
output "boundary" { value = local.boundary }
