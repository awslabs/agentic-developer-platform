# Tools platform release identity (#7513): the AgentCore gateway Terraform, the
# tools façade service and the interceptor Lambda, released from the protected
# `adp-tools-deploy-<environment>` GitHub environment. Mirrors the gateway and
# context deployment profiles: one OIDC trust pinned to that environment, an
# explicit action ceiling, exact resource names, and namespaced Kubernetes access.
# Nothing here can touch other roles, other repositories, other functions or
# other Terraform states, and key destruction is denied outright.
variable "enable_tools_deployment" {
  type    = bool
  default = false
}

variable "tools_gateway_arn" {
  description = "Existing AgentCore gateway ARN from the reviewed bootstrap state; no wildcard or automatic discovery."
  type        = string
  default     = ""
  validation {
    condition     = var.tools_gateway_arn == "" || can(regex("^arn:aws:bedrock-agentcore:${var.aws_region}:[0-9]{12}:gateway/[A-Za-z0-9_-]+$", var.tools_gateway_arn))
    error_message = "Select exactly one existing tools gateway ARN."
  }
}

variable "tools_gateway_target_arns" {
  description = "Exact existing target ARNs approved for ordinary configuration updates."
  type        = list(string)
  default     = []
  validation {
    condition     = alltrue([for arn in var.tools_gateway_target_arns : startswith(arn, "${var.tools_gateway_arn}/target/") && can(regex("^arn:aws:bedrock-agentcore:${var.aws_region}:[0-9]{12}:gateway/[A-Za-z0-9_-]+/target/[A-Za-z0-9_-]+$", arn))])
    error_message = "Each selected target must belong to the selected existing gateway."
  }
}

variable "tools_inputs_secret_arn" {
  description = "Optional existing private tools inputs secret; never a grant to create or edit secrets."
  type        = string
  default     = ""
  validation {
    condition     = var.tools_inputs_secret_arn == "" || can(regex("^arn:aws:secretsmanager:${var.aws_region}:[0-9]{12}:secret:adp/${var.environment}/tools/inputs-[A-Za-z0-9]+$", var.tools_inputs_secret_arn))
    error_message = "Supply only the existing tools inputs secret ARN."
  }
}

# Resolve the provider-owned environment key without granting key discovery to
# the release role. Lambda's default encryption still calls KMS as the reader.
data "aws_kms_key" "tools_lambda_environment" {
  count  = var.enable_tools_deployment ? 1 : 0
  key_id = "alias/aws/lambda"
}

locals {
  tools_account            = data.aws_caller_identity.current.account_id
  tools_cluster            = "arn:aws:eks:${var.aws_region}:${local.tools_account}:cluster/${var.cluster_name}"
  tools_repository         = "arn:aws:ecr:${var.aws_region}:${local.tools_account}:repository/adp-tools-facade"
  tools_gateway_repository = "arn:aws:ecr:${var.aws_region}:${local.tools_account}:repository/adp-gateway"
  tools_state_bucket       = "arn:aws:s3:::adp-terraform-state-${local.tools_account}"
  tools_state_key          = "tools/platform/${var.environment}.tfstate"
  tools_state_object       = "${local.tools_state_bucket}/${local.tools_state_key}"
  tools_state_lock         = "${local.tools_state_object}.tflock"
  tools_function           = "arn:aws:lambda:${var.aws_region}:${local.tools_account}:function:adp-tools-${var.environment}-interceptor"

  tools_lambda_environment_key = var.enable_tools_deployment ? data.aws_kms_key.tools_lambda_environment[0].arn : ""

  # Deployment context and release receipts (#7624); bucket and prefix match modules/gateway/infra/deployment-evidence.
  tools_evidence_objects = "arn:aws:s3:::adp-${var.environment}-deployment-evidence-${local.tools_account}/deployment-evidence/v1/*"
  # Shared AgentCore tools Lambda (web search, code interpreter, browser): the release root only reads it to verify it trusts this gateway.
  tools_shared_function = "arn:aws:lambda:${var.aws_region}:${local.tools_account}:function:adp-${var.environment}-shared-tools-shared"
  # Both protected release consumers inspect the shared provider environment:
  # Terraform checks gateway trust; the façade checks cleanup readiness.
  tools_environment_functions = [local.tools_function, local.tools_shared_function]
  tools_shared_function_writes = [
    "lambda:CreateFunction", "lambda:DeleteFunction", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration",
    "lambda:PublishVersion", "lambda:CreateAlias", "lambda:UpdateAlias", "lambda:DeleteAlias", "lambda:AddPermission",
    "lambda:RemovePermission", "lambda:PutFunctionConcurrency", "lambda:TagResource", "lambda:UntagResource",
  ]
  tools_build_project = "arn:aws:codebuild:${var.aws_region}:${local.tools_account}:project/adp-tools-${var.environment}-facade-build"
  tools_build         = "arn:aws:codebuild:${var.aws_region}:${local.tools_account}:build/adp-tools-${var.environment}-facade-build:*"
  tools_build_source  = "${local.tools_state_bucket}/codebuild/src/adp-tools-${var.environment}-facade-build/*"
  tools_ssm_release = [for key in [
    "gateway-id", "gateway-arn", "gateway-url", "kms-key-arn", "caller-role-arn",
    "target-role-arn", "targets", "assertion-signing-key-arn", "execution-table-name",
    "shared-tools-lambda-arn", "release",
  ] : "arn:aws:ssm:${var.aws_region}:${local.tools_account}:parameter/adp/${var.environment}/tools/${key}"]
  tools_ssm_read_only = [
    "arn:aws:ssm:${var.aws_region}:${local.tools_account}:parameter/adp/${var.environment}/tools/reconciler-signing-key-arn",
    "arn:aws:ssm:${var.aws_region}:${local.tools_account}:parameter/adp/${var.environment}/gateway/internal-plane-alb-dns",
    "arn:aws:ssm:${var.aws_region}:${local.tools_account}:parameter/adp/${var.environment}/gateway/apigw-invoke-url",
  ]
  tools_ssm_read          = concat(local.tools_ssm_release, local.tools_ssm_read_only)
  tools_gateway_resources = concat([var.tools_gateway_arn], var.tools_gateway_target_arns)
  tools_gateway_role      = "arn:aws:iam::${local.tools_account}:role/adp-tools-${var.environment}-gateway"
  tools_resource_grants = concat([
    { Sid = "CallerIdentity", Effect = "Allow", Action = ["sts:GetCallerIdentity", "ecr:GetAuthorizationToken"], Resource = "*" },
    { Sid = "ClusterDescribe", Effect = "Allow", Action = ["eks:DescribeCluster"], Resource = local.tools_cluster },
    { Sid = "Repository", Effect = "Allow", Action = [
      "ecr:DescribeRepositories", "ecr:DescribeImages", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
      "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload", "ecr:PutImage",
    ], Resource = local.tools_repository },
    { Sid = "GatewayImageRead", Effect = "Allow", Action = ["ecr:DescribeImages"], Resource = local.tools_gateway_repository },
    { Sid = "BuildProject", Effect = "Allow", Action = ["codebuild:StartBuild", "codebuild:BatchGetProjects"], Resource = local.tools_build_project },
    { Sid = "BuildEvidence", Effect = "Allow", Action = ["codebuild:BatchGetBuilds"], Resource = local.tools_build_project },
    { Sid = "StateBucket", Effect = "Allow", Action = ["s3:ListBucket"], Resource = local.tools_state_bucket,
    Condition = { StringLike = { "s3:prefix" = [local.tools_state_key, "${local.tools_state_key}.tflock"] } } },
    { Sid = "ReleaseState", Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = local.tools_state_object },
    { Sid = "ReleaseStateLock", Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = local.tools_state_lock },
    { Sid = "BuildSource", Effect = "Allow", Action = ["s3:PutObject"], Resource = local.tools_build_source },
    # Share exact ARN sets without repeating ten release keys across both grants.
    { Sid = "ReadReleaseInputs", Effect = "Allow", Action = ["ssm:GetParameter"], Resource = local.tools_ssm_read_only },
    { Sid = "PublishReleaseContext", Effect = "Allow", Action = ["ssm:GetParameter", "ssm:PutParameter"], Resource = local.tools_ssm_release },
    { Sid = "InterceptorCode", Effect = "Allow", Action = [
      "lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionCode", "lambda:PublishVersion",
      "lambda:GetAlias", "lambda:CreateAlias", "lambda:UpdateAlias", "lambda:ListVersionsByFunction",
    ], Resource = [local.tools_function, "${local.tools_function}:*"] },
    { Sid = "ToolsEnvironmentRead", Effect = "Allow", Action = ["kms:Decrypt"], Resource = local.tools_lambda_environment_key,
      Condition = { StringEquals = {
        "kms:ViaService"                               = "lambda.${var.aws_region}.amazonaws.com"
        "kms:CallerAccount"                            = local.tools_account
        "kms:EncryptionContext:aws:lambda:FunctionArn" = local.tools_environment_functions
      } }
    },
    { Sid = "SharedToolsRead", Effect = "Allow", Action = [
      "lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:ListVersionsByFunction", "lambda:GetAlias", "lambda:ListAliases", "lambda:GetPolicy", "lambda:ListTags",
    ], Resource = [local.tools_shared_function, "${local.tools_shared_function}:*"] },
    { Sid = "GatewayConfiguration", Effect = "Allow", Action = [
      "bedrock-agentcore:GetGateway", "bedrock-agentcore:UpdateGateway", "bedrock-agentcore:GetResourcePolicy",
      "bedrock-agentcore:PutResourcePolicy", "bedrock-agentcore:ListTagsForResource",
    ], Resource = var.tools_gateway_arn },
    # UpdateGateway requires passing its existing service role even when only
    # protocol settings change. The saved plan also pins this exact role.
    { Sid = "GatewayPassRole", Effect = "Allow", Action = ["iam:PassRole"], Resource = local.tools_gateway_role,
    Condition = { StringEquals = { "iam:PassedToService" = "bedrock-agentcore.amazonaws.com" } } },
    { Sid = "TargetConfiguration", Effect = "Allow", Action = [
      "bedrock-agentcore:GetGatewayTarget", "bedrock-agentcore:UpdateGatewayTarget",
      "bedrock-agentcore:ListGatewayTargets", "bedrock-agentcore:SynchronizeGatewayTargets",
    ], Resource = local.tools_gateway_resources },
    ], var.tools_inputs_secret_arn == "" ? [] : [
    { Sid = "ToolsInputsOnly", Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = var.tools_inputs_secret_arn },
  ])
  tools_actions = distinct(concat(flatten([for statement in local.tools_resource_grants : statement.Action]), local.tools_knowledge_actions, local.tools_gateway_key_actions, local.tools_adapters_actions))
  # Keep the PassRole grant and its explicit resource/service fences in the
  # same atomic inline update, without exceeding the managed ceiling quota.
  tools_gateway_role_denials = [
    { Sid = "DenyOtherPassedRoles", Effect = "Deny", Action = "iam:PassRole", NotResource = local.tools_gateway_role },
    { Sid = "DenyOtherGatewayRoleService", Effect = "Deny", Action = "iam:PassRole", Resource = local.tools_gateway_role,
    Condition = { StringNotEquals = { "iam:PassedToService" = "bedrock-agentcore.amazonaws.com" } } },
  ]
  # The AWS-managed key policy independently allows account-wide decryption.
  # Conditional Allow alone is insufficient: keep explicit fences in the same
  # inline document as its exception to the action ceiling, updated atomically.
  tools_environment_denials = [
    { Sid = "DenyOtherEnvironmentKeys", Effect = "Deny", Action = "kms:Decrypt", NotResource = concat([local.tools_lambda_environment_key], local.tools_gateway_key_enabled ? [var.tools_gateway_kms_key_arn] : []) },
    { Sid = "DenyNonLambdaEnvironmentDecrypt", Effect = "Deny", Action = "kms:Decrypt", Resource = local.tools_lambda_environment_key,
    Condition = { StringNotEquals = { "kms:ViaService" = "lambda.${var.aws_region}.amazonaws.com" } } },
    { Sid = "DenyOtherFunctionEnvironments", Effect = "Deny", Action = "kms:Decrypt", Resource = local.tools_lambda_environment_key,
    Condition = { StringNotEquals = { "kms:EncryptionContext:aws:lambda:FunctionArn" = local.tools_environment_functions } } },
  ]
}

resource "aws_iam_role" "tools_deployment" {
  count                = var.enable_tools_deployment ? 1 : 0
  name                 = "${var.name_prefix}-tools-trusted-deployment"
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:${var.repository}:environment:adp-tools-deploy-${var.environment}",
    } },
  }] })
}

resource "aws_iam_role_policy" "tools_deployment" {
  count = var.enable_tools_deployment ? 1 : 0
  name  = "tools-platform-release"
  role  = aws_iam_role.tools_deployment[0].id
  # Legacy installations keep their action denial in this same document. Retain
  # it atomically when replacing the broad legacy grants, before the standalone
  # ceiling exists. The duplicate denial also preserves already-owned identities.
  policy = jsonencode({ Version = "2012-10-17", Statement = concat(local.tools_resource_grants, local.tools_environment_denials, local.tools_gateway_role_denials, [
    { Sid = "DenyOtherAPIs", Effect = "Deny", NotAction = local.tools_actions, Resource = "*" }
  ]) })
  depends_on = [aws_iam_role_policy_attachment.tools_deployment_resource_ceiling, aws_iam_role_policy_attachment.tools_knowledge_creation, aws_iam_role_policy_attachment.tools_gateway_key, aws_iam_role_policy_attachment.tools_adapters_publication]
  lifecycle {
    precondition {
      condition     = var.tools_gateway_arn != "" && startswith(var.tools_gateway_arn, "arn:aws:bedrock-agentcore:${var.aws_region}:${local.tools_account}:gateway/")
      error_message = "Select the reviewed gateway from this account before enabling tools deployment."
    }
    precondition {
      condition     = var.tools_inputs_secret_arn == "" || startswith(var.tools_inputs_secret_arn, "arn:aws:secretsmanager:${var.aws_region}:${local.tools_account}:secret:")
      error_message = "Tools inputs secret must belong to this account."
    }
  }
}

# Keep the action ceiling inline and attach resource ceilings separately. IAM's
# inline quota is aggregate across the role, not per inline policy. These deny
# statements keep the same exact resources/actions; attach them before reducing
# the existing inline ceiling, so the migration never drops its resource bounds.
locals {
  tools_resource_denials = concat([
    # Both action ceilings deny IAM APIs outside exact PassRole and optional
    # network service-linked-role creation. Each has atomic resource/service fences.
    { Sid = "DenyIdentityMutation", Effect = "Deny", Action = ["sts:AssumeRole", "sts:AssumeRoleWithWebIdentity"], Resource = "*" },
    { Sid = "DenyKeyMutation", Effect = "Deny", Action = ["kms:PutKeyPolicy", "kms:CreateKey", "kms:ScheduleKeyDeletion", "kms:DisableKey"], Resource = "*" },
    { Sid = "DenyOtherClusters", Effect = "Deny", Action = "eks:DescribeCluster", NotResource = local.tools_cluster },
    { Sid = "DenyOtherRepositories", Effect = "Deny", Action = [
      "ecr:DescribeRepositories", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
      "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload", "ecr:PutImage",
    ], NotResource = concat([local.tools_repository], local.tools_adapters_enabled ? [local.tools_adapters_repository] : []) },
    { Sid = "DenyOtherImageReads", Effect = "Deny", Action = "ecr:DescribeImages", NotResource = concat([local.tools_repository, local.tools_gateway_repository], local.tools_adapters_enabled ? [local.tools_adapters_repository] : []) },
    { Sid = "DenyOtherFunctions", Effect = "Deny", Action = "lambda:*", NotResource = [local.tools_function, "${local.tools_function}:*", local.tools_shared_function, "${local.tools_shared_function}:*"] },
    { Sid = "DenySharedToolsWrites", Effect = "Deny", Action = local.tools_shared_function_writes, Resource = [local.tools_shared_function, "${local.tools_shared_function}:*"] },
    { Sid = "DenyOtherGatewayResources", Effect = "Deny", Action = "bedrock-agentcore:*", NotResource = local.tools_gateway_resources },
    { Sid = "DenyGatewayAndTargetDeletion", Effect = "Deny", Action = ["bedrock-agentcore:DeleteGateway", "bedrock-agentcore:DeleteGatewayTarget", "bedrock-agentcore:DeleteResourcePolicy"], Resource = "*" },
    { Sid = "DenyOtherStateAndSources", Effect = "Deny", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], NotResource = concat([local.tools_state_object, local.tools_state_lock, local.tools_build_source, local.tools_evidence_objects], local.tools_adapters_enabled ? local.tools_adapters_sources : []) },
    { Sid = "DenyEvidenceDeletion", Effect = "Deny", Action = ["s3:DeleteObject", "s3:DeleteObjectVersion"], Resource = local.tools_evidence_objects },
    { Sid = "DenyReleaseStateDeletion", Effect = "Deny", Action = "s3:DeleteObject", Resource = local.tools_state_object },
    { Sid = "DenyOtherBuckets", Effect = "Deny", Action = "s3:ListBucket", NotResource = local.tools_state_bucket },
    { Sid = "DenyOtherStatePrefixes", Effect = "Deny", Action = "s3:ListBucket", Resource = local.tools_state_bucket,
    Condition = { StringNotLike = { "s3:prefix" = [local.tools_state_key, "${local.tools_state_key}.tflock"] } } },
    # Equivalent ceiling: both actions denied outside the full read set, and
    # writes additionally denied on its two read-only keys. This avoids another
    # copy of the ten release ARNs within the role's aggregate inline quota.
    { Sid = "DenyOtherParameterReads", Effect = "Deny", Action = ["ssm:GetParameter", "ssm:PutParameter"], NotResource = local.tools_ssm_read },
    { Sid = "DenyOtherParameterWrites", Effect = "Deny", Action = "ssm:PutParameter", Resource = local.tools_ssm_read_only },
    ], local.tools_adapters_enabled ? [] : [
    { Sid = "DenyOtherBuilds", Effect = "Deny", Action = ["codebuild:StartBuild", "codebuild:BatchGetProjects", "codebuild:BatchGetBuilds"], NotResource = [local.tools_build_project, local.tools_build] },
    ], var.tools_inputs_secret_arn == "" ? [] : [
    { Sid = "DenyOtherSecrets", Effect = "Deny", Action = "secretsmanager:GetSecretValue", NotResource = var.tools_inputs_secret_arn },
  ])
}

resource "aws_iam_policy" "tools_deployment_resource_ceiling" {
  count  = var.enable_tools_deployment ? 1 : 0
  name   = "${var.name_prefix}-tools-release-resource-ceiling"
  policy = jsonencode({ Version = "2012-10-17", Statement = local.tools_resource_denials })
  # Optional adapter fences must exist before allowing its source/repository
  # through the shared resource ceiling. They depend only on the role itself.
  depends_on = [aws_iam_role_policy_attachment.tools_adapters_publication]
  lifecycle {
    precondition {
      condition     = length(jsonencode({ Version = "2012-10-17", Statement = local.tools_resource_denials })) <= 6144
      error_message = "Exact tools resource ceilings exceed the managed-policy quota; split them without broadening authority."
    }
  }
}

resource "aws_iam_role_policy_attachment" "tools_deployment_resource_ceiling" {
  count      = var.enable_tools_deployment ? 1 : 0
  role       = aws_iam_role.tools_deployment[0].name
  policy_arn = aws_iam_policy.tools_deployment_resource_ceiling[0].arn
}

resource "aws_iam_role_policy" "tools_deployment_ceiling" {
  count = var.enable_tools_deployment ? 1 : 0
  name  = "tools-platform-denials"
  role  = aws_iam_role.tools_deployment[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "DenyOtherAPIs", Effect = "Deny", NotAction = local.tools_actions, Resource = "*" }
  ] })
  # Shrink the existing combined legacy inline policy first. Creating this
  # additional inline document while the old one remains can exceed 10,240 bytes.
  depends_on = [aws_iam_role_policy.tools_deployment]
}

resource "aws_eks_access_entry" "tools_deployment" {
  count             = var.enable_tools_deployment ? 1 : 0
  cluster_name      = var.cluster_name
  principal_arn     = aws_iam_role.tools_deployment[0].arn
  type              = "STANDARD"
  kubernetes_groups = ["adp:tools-release-${var.environment}"]
}

output "tools_deployment_role_arn" {
  value = var.enable_tools_deployment ? aws_iam_role.tools_deployment[0].arn : null
}
