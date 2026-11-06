# Façade release surface (#7513): the IRSA role the façade pods run as, the
# assertion signing key they use, and the CodeBuild project that publishes the
# façade image. All three are opt-in through `eks_cluster_name` / `state_bucket`
# so the gateway-only plan and tests keep their shape.

variable "eks_oidc_provider_arn" {
  description = "IAM OIDC provider of the EKS cluster hosting the tools façade; empty leaves the façade identity out of this stack. A plain variable so a read-only plan needs no EKS access."
  type        = string
  default     = ""
  validation {
    condition     = var.eks_oidc_provider_arn == "" || can(regex("^arn:aws:iam::${var.aws_account_id}:oidc-provider/oidc\\.eks\\.${var.aws_region}\\.amazonaws\\.com/id/[A-F0-9]+$", var.eks_oidc_provider_arn))
    error_message = "Supply the EKS OIDC provider ARN of a cluster in the target account and region."
  }
}

variable "state_bucket" {
  description = "Terraform/CodeBuild source bucket; empty leaves the façade image build project out of this stack."
  type        = string
  default     = ""
  validation {
    condition     = var.state_bucket == "" || can(regex("^[a-z0-9.-]{3,63}$", var.state_bucket))
    error_message = "Supply a plain S3 bucket name."
  }
}

locals {
  facade_enabled      = var.eks_oidc_provider_arn != ""
  facade_build        = var.state_bucket != ""
  facade_role_name    = "adp-${var.environment}-role-tools-facade"
  facade_build_name   = "adp-tools-${var.environment}-facade-build"
  facade_issuer       = local.facade_enabled ? split("oidc-provider/", var.eks_oidc_provider_arn)[1] : ""
  assertion_key_count = (local.facade_enabled || var.interceptor_lambda_arn != "" || var.enable_interceptor || var.credential_target_url != "") ? 1 : 0
}

resource "aws_iam_role" "facade" {
  count = local.facade_enabled ? 1 : 0
  name  = local.facade_role_name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = var.eks_oidc_provider_arn }
      Condition = { StringEquals = {
        "${local.facade_issuer}:aud" = "sts.amazonaws.com"
        "${local.facade_issuer}:sub" = "system:serviceaccount:adp-gateway:tools-facade"
      } }
    }]
  })
  lifecycle {
    precondition {
      condition     = var.tools_facade_role_arn == "arn:aws:iam::${var.aws_account_id}:role/${local.facade_role_name}"
      error_message = "The façade role this stack creates must be the caller the gateway policy admits."
    }
  }
}

resource "aws_iam_role_policy" "facade" {
  count = local.facade_enabled ? 1 : 0
  name  = "adp-tools-facade-runtime"
  role  = aws_iam_role.facade[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "InvokeToolsGateway", Effect = "Allow", Action = "bedrock-agentcore:InvokeGateway", Resource = var.gateway_arn },
      # The façade authorises every human tool call through the ADP gateway's
      # internal plane (API Gateway, IAM auth). Without this grant every call is
      # refused before the gateway sees it.
      { Sid = "InvokeGatewayToolsAuthorize", Effect = "Allow", Action = "execute-api:Invoke", Resource = [
        "${var.gateway_internal_api_execution_arn}/POST/internal/v1/tools/authorize",
        "${var.gateway_internal_api_execution_arn}/POST/internal/v1/tools/native-dispatch",
        "${var.gateway_internal_api_execution_arn}/POST/internal/v1/tools/l5-list",
        "${var.gateway_internal_api_execution_arn}/POST/internal/v1/tools/l5-call",
      ] },
      { Sid = "SignPrincipalAssertions", Effect = "Allow", Action = "kms:Sign", Resource = aws_kms_key.assertion[0].arn },
      { Sid = "OwnExecutionRecords", Effect = "Allow", Action = [
        "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan",
        "dynamodb:ConditionCheckItem", "dynamodb:TransactWriteItems", "dynamodb:TransactGetItems",
      ], Resource = [aws_dynamodb_table.executions.arn, "${aws_dynamodb_table.executions.arn}/index/*"] },
    ]
  })
}

resource "aws_cloudwatch_log_group" "facade_build" {
  count             = local.facade_build ? 1 : 0
  name              = "/aws/codebuild/${local.facade_build_name}"
  retention_in_days = 30
}

resource "aws_iam_role" "facade_build" {
  count = local.facade_build ? 1 : 0
  name  = local.facade_build_name
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "codebuild.amazonaws.com" }, Action = "sts:AssumeRole",
    Condition = { StringEquals = {
      "aws:SourceAccount" = var.aws_account_id,
      "aws:SourceArn"     = "arn:aws:codebuild:${var.aws_region}:${var.aws_account_id}:project/${local.facade_build_name}",
    } },
  }] })
}

resource "aws_iam_role_policy" "facade_build" {
  count = local.facade_build ? 1 : 0
  name  = "build-scope"
  role  = aws_iam_role.facade_build[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "OwnBuildLogs", Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      Resource = [aws_cloudwatch_log_group.facade_build[0].arn, "${aws_cloudwatch_log_group.facade_build[0].arn}:*"] },
      { Sid = "BuildSourceRead", Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"],
      Resource = "arn:aws:s3:::${var.state_bucket}/codebuild/src/${local.facade_build_name}/*" },
      { Sid = "EcrAuth", Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      { Sid = "OwnEcrRepository", Effect = "Allow", Action = [
        "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload", "ecr:DescribeImages",
        "ecr:DescribeRepositories", "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload", "ecr:ListImages",
        "ecr:PutImage", "ecr:UploadLayerPart",
      ], Resource = aws_ecr_repository.facade.arn },
      { Sid = "DenyOtherRepositories", Effect = "Deny", NotAction = "ecr:GetAuthorizationToken", NotResource = aws_ecr_repository.facade.arn,
      Condition = { "ForAnyValue:StringLike" = { "aws:CalledVia" = ["ecr.amazonaws.com"] } } },
    ]
  })
}

resource "aws_codebuild_project" "facade_build" {
  count         = local.facade_build ? 1 : 0
  name          = local.facade_build_name
  description   = "ADP tools façade image publication from a reviewed source archive"
  service_role  = aws_iam_role.facade_build[0].arn
  build_timeout = 30

  artifacts {
    type = "NO_ARTIFACTS"
  }

  source {
    type      = "S3"
    location  = "${var.state_bucket}/codebuild/src/${local.facade_build_name}/explicit-source-required.zip"
    buildspec = "codebuild/bs-tools-facade-build.yml"
  }

  environment {
    type                        = "LINUX_CONTAINER"
    image                       = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    compute_type                = "BUILD_GENERAL1_MEDIUM"
    privileged_mode             = true # docker build of the façade image before push
    image_pull_credentials_type = "CODEBUILD"

    environment_variable {
      name  = "ACCOUNT_ID"
      value = var.aws_account_id
    }
    environment_variable {
      name  = "REGISTRY"
      value = "${var.aws_account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
    }
  }

  logs_config {
    cloudwatch_logs {
      group_name  = aws_cloudwatch_log_group.facade_build[0].name
      stream_name = ""
    }
  }
}

output "facade_role_arn" {
  description = "IRSA role of the tools façade pods; empty until the façade identity is enabled."
  value       = local.facade_enabled ? aws_iam_role.facade[0].arn : ""
}

output "facade_build_project_name" {
  description = "CodeBuild project that publishes the façade image; empty until enabled."
  value       = local.facade_build ? aws_codebuild_project.facade_build[0].name : ""
}
