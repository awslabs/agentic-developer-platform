# The shared runner executes repository input. Deployment is a different
# identity. Use the same effective ceiling in active and legacy installations.
variable "account_id" { type = string }
variable "aws_region" { type = string }
variable "name_prefix" { type = string }
variable "environment" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "Use the deployment environment, independently of the runner role name."
  }
}
variable "gateway_execution_arns" {
  type        = list(string)
  default     = []
  description = "Operator-reviewed gateway routes. Empty disables gateway transport. API ID, stage and method must be exact."
  validation {
    condition     = alltrue([for arn in var.gateway_execution_arns : can(regex("^arn:aws:execute-api:[a-z0-9-]+:[0-9]{12}:[a-z0-9]+/[A-Za-z0-9_$-]+/(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)/(agent|internal)/([A-Za-z0-9_/-]+|\\*)$", arn))])
    error_message = "Use exact API/stage/method execution ARNs for required agent/internal routes; only the final route suffix may be a wildcard."
  }
}
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
variable "transport_secret_kms_arns" {
  type        = list(string)
  default     = []
  description = "Exact KMS key ARNs that encrypt the transport secrets. Required for decryption once the default-deny boundary is applied. The operator obtains these from each secret's KmsKeyId (or alias/aws/secretsmanager's resolved key ARN)."
  validation {
    condition = alltrue([for arn in var.transport_secret_kms_arns :
      can(regex("^arn:aws:kms:[a-z0-9-]+:[0-9]{12}:key/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|mrk-[0-9a-f]{32})$", arn)) &&
      !can(regex("[*?]", arn))
    ])
    error_message = "KMS key ARNs must be exact resolved key ARNs (not aliases); broader inputs are forbidden."
  }
}

locals {
  resource_prefix = "adp-${var.environment}"
  gateway_pr_role = "arn:aws:iam::${var.account_id}:role/${local.resource_prefix}-codebuild-gateway-pr"
  capabilities = {
    StartSmokeBuild = {
      actions   = ["codebuild:StartBuild"]
      resources = ["arn:aws:codebuild:${var.aws_region}:${var.account_id}:project/${local.resource_prefix}-gateway-build"]
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
        "arn:aws:codebuild:${var.aws_region}:${var.account_id}:project/${local.resource_prefix}-gateway-build",
        "arn:aws:codebuild:${var.aws_region}:${var.account_id}:build/${local.resource_prefix}-gateway-build:*",
      ]
    }
    OwnSmokeSource = {
      actions = ["s3:PutObject", "s3:GetObject"]
      resources = [
        "arn:aws:s3:::adp-terraform-state-${var.account_id}/codebuild/src/${local.resource_prefix}-gateway-build-pr/*",
      ]
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
      resources = ["arn:aws:ssm:${var.aws_region}:${var.account_id}:parameter/adp/${var.environment}/gateway/apigw-invoke-url"]
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
  # The default-deny boundary blocks all APIs outside the enumerated ceiling,
  # including kms:Decrypt. Secrets encrypted with any key (including the
  # AWS-managed alias/aws/secretsmanager) fail retrieval without this grant.
  # Conditions pin decryption to Secrets Manager and to the exact transport
  # secret encryption contexts; boundary denies below make these unoverridable.
  kms_service = "secretsmanager.${var.aws_region}.amazonaws.com"
  secret_decrypt = length(var.transport_secret_kms_arns) == 0 ? {} : {
    SecretDecryption = {
      actions   = ["kms:Decrypt"]
      resources = var.transport_secret_kms_arns
      condition = { StringEquals = {
        "kms:ViaService"                  = local.kms_service
        "kms:EncryptionContext:SecretARN" = var.transport_secret_arns
      } }
    }
  }
  gateway = length(var.gateway_execution_arns) == 0 ? {} : {
    GatewayTransport = {
      actions   = ["execute-api:Invoke"]
      resources = var.gateway_execution_arns
    }
  }
  allowed = merge(local.capabilities, local.transport, local.gateway, local.secret_decrypt)
  grants = [for name, capability in local.allowed : merge({
    Sid = name, Effect = "Allow", Action = capability.actions, Resource = capability.resources
  }, try({ Condition = capability.condition }, {}))]
  # When KMS decrypt is in the ceiling, pin it to Secrets Manager and the exact
  # transport encryption contexts. Without these, another attached or resource
  # policy could grant direct decryption or use the key for unrelated secrets.
  kms_boundary_denies = [for entry in [
    { sid = "DenyDirectKeyDecryption", key = "kms:ViaService", values = local.kms_service },
    { sid = "DenyOtherSecretDecryption", key = "kms:EncryptionContext:SecretARN", values = var.transport_secret_arns },
    ] : {
    Sid       = entry.sid, Effect = "Deny", Action = ["kms:Decrypt"], Resource = "*",
    Condition = { StringNotEquals = { (entry.key) = entry.values } }
  } if length(var.transport_secret_kms_arns) > 0]
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
    }
    ], [for name, capability in local.allowed : {
      Sid    = "DenyOther${name}Resources", Effect = "Deny",
      Action = capability.actions, NotResource = capability.resources
  } if capability.resources != ["*"]], local.kms_boundary_denies)
}

output "grants" { value = local.grants }
output "boundary" { value = local.boundary }
