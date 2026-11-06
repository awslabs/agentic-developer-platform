variable "enable_shared_tools_deployment" {
  type    = bool
  default = false
}

variable "shared_tools_state_bucket" {
  type    = string
  default = ""
}

variable "shared_tools_state_key" {
  description = "Exact pre-existing shared AgentCore state key; supplied privately, never generated from the environment."
  type        = string
  default     = ""
}

variable "shared_tools_resource_name" {
  description = "Exact existing shared-provider Terraform name prefix."
  type        = string
  default     = ""
}

variable "shared_tools_lambda_repository" {
  type    = string
  default = ""
}

variable "shared_tools_browser_repository" {
  type    = string
  default = ""
}

variable "shared_tools_dynamodb_kms_key_arn" {
  description = "Exact existing regional alias/aws/dynamodb backing key ARN, verified privately before enabling the provider's metadata-only refresh permission."
  type        = string
  default     = ""
  validation {
    condition = var.shared_tools_dynamodb_kms_key_arn == "" || can(regex(
      "^arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:key/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
      var.shared_tools_dynamodb_kms_key_arn
    ))
    error_message = "Select the exact existing DynamoDB KMS key ARN in the approved account and region; aliases and wildcard resources are not allowed."
  }
}

variable "shared_tools_legacy_rest_api_id" {
  description = "Existing shared tools REST API ID for read-only retention; omit for new installations."
  type        = string
  default     = ""
  validation {
    condition     = var.shared_tools_legacy_rest_api_id == "" || can(regex("^[a-z0-9]+$", var.shared_tools_legacy_rest_api_id))
    error_message = "Select the exact existing tools REST API ID."
  }
}

variable "shared_tools_legacy_worker_role_arns" {
  description = "Exact existing full worker-role ARN keys for read-only Terraform refresh."
  type        = set(string)
  default     = []
  validation {
    condition = alltrue([for arn in var.shared_tools_legacy_worker_role_arns :
      can(regex("^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$", arn)) &&
      startswith(arn, "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/")
    ])
    error_message = "Select exact worker role ARNs from the approved target."
  }
}

locals {
  shared_account  = data.aws_caller_identity.current.account_id
  shared_build    = "adp-${var.environment}-shared-tools-build"
  shared_bucket   = "arn:aws:s3:::${var.shared_tools_state_bucket}"
  shared_state    = "${local.shared_bucket}/${var.shared_tools_state_key}"
  shared_function = "arn:aws:lambda:${var.aws_region}:${local.shared_account}:function:${var.shared_tools_resource_name}-shared"
  shared_resources = [for suffix in ["shared-lambda", "browser-service", "websearch-gateway"] :
  "arn:aws:iam::${local.shared_account}:role/${var.shared_tools_resource_name}-${suffix}"]
  shared_repositories = [for name in [var.shared_tools_lambda_repository, var.shared_tools_browser_repository] :
  "arn:aws:ecr:${var.aws_region}:${local.shared_account}:repository/${name}"]
}

resource "aws_iam_role" "shared_tools_deployment" {
  count                = var.enable_shared_tools_deployment ? 1 : 0
  name                 = "${var.name_prefix}-shared-tools-trusted-deployment"
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:${var.repository}:environment:adp-shared-tools-deploy-${var.environment}",
    } },
  }] })
  lifecycle {
    precondition {
      condition = (
        can(regex("^[a-z0-9.-]{3,63}$", var.shared_tools_state_bucket)) &&
        can(regex("^[a-zA-Z0-9_./-]*tools/agentcore/[a-zA-Z0-9_./-]+[.]tfstate$", var.shared_tools_state_key)) &&
        !strcontains(var.shared_tools_state_key, "..") &&
        can(regex("^[a-zA-Z0-9-]+$", var.shared_tools_resource_name)) &&
        var.shared_tools_resource_name != "" &&
        alltrue([for name in [var.shared_tools_lambda_repository, var.shared_tools_browser_repository] :
        can(regex("^[a-zA-Z0-9_./-]+$", name)) && name != "" && !strcontains(name, "..")]) &&
        var.shared_tools_lambda_repository != var.shared_tools_browser_repository
      )
      error_message = "Coordinator must bind the exact existing shared state, resource name and two repositories before enabling the separate release role."
    }
  }
}

resource "aws_iam_role_policy" "shared_tools_deployment" {
  count = var.enable_shared_tools_deployment ? 1 : 0
  name  = "shared-tools-provider-release"
  role  = aws_iam_role.shared_tools_deployment[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["sts:GetCallerIdentity", "ecr:GetAuthorizationToken"], Resource = "*" },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = local.shared_bucket,
    Condition = { StringLike = { "s3:prefix" = [var.shared_tools_state_key, "${var.shared_tools_state_key}.tflock", "codebuild/src/${local.shared_build}/*", "codebuild/artifacts/shared-agentcore-tools/*"] } } },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = [local.shared_state, "${local.shared_bucket}/codebuild/src/${local.shared_build}/*", "${local.shared_bucket}/codebuild/artifacts/shared-agentcore-tools/*"] },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = "${local.shared_state}.tflock" },
    { Effect = "Allow", Action = ["codebuild:StartBuild", "codebuild:BatchGetProjects"], Resource = "arn:aws:codebuild:${var.aws_region}:${local.shared_account}:project/${local.shared_build}" },
    # CodeBuild authorizes polling/stopping a build against its project, not its build ARN.
    { Effect = "Allow", Action = ["codebuild:BatchGetBuilds", "codebuild:StopBuild"], Resource = "arn:aws:codebuild:${var.aws_region}:${local.shared_account}:project/${local.shared_build}" },
    { Effect = "Allow", Action = ["ecr:DescribeImages", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], Resource = local.shared_repositories },
    { Effect = "Allow", Action = "eks:DescribeCluster", Resource = "arn:aws:eks:${var.aws_region}:${local.shared_account}:cluster/${var.cluster_name}" },
    { Effect = "Allow", Action = ["lambda:CreateFunction", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration", "lambda:DeleteFunction", "lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:GetPolicy", "lambda:PutFunctionConcurrency", "lambda:ListVersionsByFunction", "lambda:GetAlias", "lambda:ListAliases", "lambda:AddPermission", "lambda:RemovePermission", "lambda:ListTags", "lambda:TagResource"], Resource = [local.shared_function, "${local.shared_function}:*"] },
    { Effect = "Allow", Action = ["iam:CreateRole", "iam:GetRole", "iam:PutRolePolicy", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:ListRoleTags", "iam:DeleteRolePolicy", "iam:DeleteRole", "iam:PassRole"], Resource = local.shared_resources },
    { Effect = "Allow", Action = ["dynamodb:CreateTable", "dynamodb:DescribeTable", "dynamodb:UpdateTable", "dynamodb:DeleteTable", "dynamodb:DescribeContinuousBackups", "dynamodb:DescribeTimeToLive", "dynamodb:ListTagsOfResource", "dynamodb:UpdateContinuousBackups"], Resource = [for suffix in ["operations", "browser-operations"] : "arn:aws:dynamodb:${var.aws_region}:${local.shared_account}:table/${var.shared_tools_resource_name}-${suffix}"] },
    { Effect = "Allow", Action = ["sqs:CreateQueue", "sqs:GetQueueAttributes", "sqs:GetQueueUrl", "sqs:SetQueueAttributes", "sqs:DeleteQueue", "sqs:ListQueueTags", "sqs:TagQueue"], Resource = [for suffix in ["browser.fifo", "browser-dead.fifo"] : "arn:aws:sqs:${var.aws_region}:${local.shared_account}:${var.shared_tools_resource_name}-${suffix}"] },
    { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:PutRetentionPolicy", "logs:DeleteLogGroup"], Resource = "arn:aws:logs:${var.aws_region}:${local.shared_account}:log-group:/aws/lambda/${var.shared_tools_resource_name}-shared" },
    { Effect = "Allow", Action = ["logs:DescribeLogGroups"], Resource = "*" },
    { Effect = "Allow", Action = "logs:ListTagsForResource", Resource = [
      "arn:aws:logs:${var.aws_region}:${local.shared_account}:log-group:/aws/lambda/${var.shared_tools_resource_name}-shared",
      "arn:aws:logs:${var.aws_region}:${local.shared_account}:log-group:/aws/lambda/${var.shared_tools_resource_name}-shared:*",
    ] },
    { Effect = "Allow", Action = ["events:PutRule", "events:DescribeRule", "events:ListTargetsByRule", "events:ListTagsForResource", "events:DeleteRule", "events:PutTargets", "events:RemoveTargets"], Resource = "arn:aws:events:${var.aws_region}:${local.shared_account}:rule/${var.shared_tools_resource_name}-interpreter-cleanup" },
    { Effect = "Allow", Action = ["ec2:DescribeVpcs", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups", "ec2:DescribeNetworkInterfaces"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } } },
    ], [for permission in [
      { Effect = "Allow", Action = "apigateway:GET", Resource = ["arn:aws:apigateway:${var.aws_region}::/restapis/${var.shared_tools_legacy_rest_api_id}", "arn:aws:apigateway:${var.aws_region}::/restapis/${var.shared_tools_legacy_rest_api_id}/*"] },
      # The provider refreshes the latest version even for retained image functions.
      { Effect = "Allow", Action = ["lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:GetPolicy", "lambda:ListTags", "lambda:ListVersionsByFunction", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration"], Resource = "arn:aws:lambda:${var.aws_region}:${local.shared_account}:function:${var.shared_tools_resource_name}-browser" },
      { Effect = "Allow", Action = ["iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies"], Resource = "arn:aws:iam::${local.shared_account}:role/${var.shared_tools_resource_name}-browser-gateway" },
      { Effect = "Allow", Action = ["logs:DescribeLogGroups"], Resource = "*" },
      { Effect = "Allow", Action = "logs:ListTagsForResource", Resource = [
        "arn:aws:logs:${var.aws_region}:${local.shared_account}:log-group:/aws/lambda/${var.shared_tools_resource_name}-browser",
        "arn:aws:logs:${var.aws_region}:${local.shared_account}:log-group:/aws/lambda/${var.shared_tools_resource_name}-browser:*",
      ] },
      ] : permission if var.shared_tools_legacy_rest_api_id != ""], [for permission in [
      { Effect = "Allow", Action = ["iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies"], Resource = sort(tolist(var.shared_tools_legacy_worker_role_arns)) },
      ] : permission if length(var.shared_tools_legacy_worker_role_arns) > 0], [for permission in [
      # The AWS provider describes alias/aws/dynamodb to normalize the default
      # encryption key during refresh. A denied read otherwise appears as drift.
      { Effect = "Allow", Action = "kms:DescribeKey", Resource = var.shared_tools_dynamodb_kms_key_arn },
  ] : permission if var.shared_tools_dynamodb_kms_key_arn != ""]) })
}

resource "aws_eks_access_entry" "shared_tools_deployment" {
  count             = var.enable_shared_tools_deployment ? 1 : 0
  cluster_name      = var.cluster_name
  principal_arn     = aws_iam_role.shared_tools_deployment[0].arn
  type              = "STANDARD"
  kubernetes_groups = ["adp:shared-tools-release-${var.environment}"]
}

output "shared_tools_deployment_role_arn" {
  value = var.enable_shared_tools_deployment ? aws_iam_role.shared_tools_deployment[0].arn : null
}
