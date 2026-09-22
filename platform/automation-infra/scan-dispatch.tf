# On-demand scanning is a separate identity, never a deployment-role exception.
variable "security_agent_space_id" {
  type    = string
  default = ""
  validation {
    condition     = var.security_agent_space_id == "" || can(regex("^as-[a-f0-9-]+$", var.security_agent_space_id))
    error_message = "Use the exact existing, reviewed agent-space ID."
  }
}
resource "aws_iam_role" "scan" {
  name                 = "${var.name_prefix}-trusted-scan"
  max_session_duration = 21600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:${var.repository}:environment:adp-scan-${var.environment}"
    } }
  }] })
}
locals {
  scan_bucket  = "arn:aws:s3:::${var.name_prefix}-security-scans-${data.aws_caller_identity.current.account_id}"
  state_bucket = "arn:aws:s3:::adp-terraform-state-${data.aws_caller_identity.current.account_id}"
}
resource "aws_iam_role_policy" "scan" {
  name = "on-demand-scanning"
  role = aws_iam_role.scan.id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    {
      Sid = "ScanEvidence", Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      Resource = concat([for prefix in ["sarif", "sbom", "findings", "repository-scans", "security-agent"] : "${local.scan_bucket}/${prefix}/*"],
      ["${local.state_bucket}/repository-scans/*"], [for tool in ["grype", "syft"] : "${local.state_bucket}/codebuild/src/${var.name_prefix}-${tool}-scan/*"])
    },
    {
      Sid       = "ListEvidence", Effect = "Allow", Action = ["s3:ListBucket"], Resource = [local.scan_bucket, local.state_bucket],
      Condition = { StringLike = { "s3:prefix" = ["sarif/*", "sbom/*", "findings/*", "repository-scans/*", "security-agent/*"] } }
    },
    {
      Sid = "VerifyPrivateScanBucket", Effect = "Allow", Action = ["s3:GetBucketPublicAccessBlock", "s3:GetBucketPolicyStatus"], Resource = local.scan_bucket
    },
    {
      Sid    = "OnlyNonPublishingScannerProjects", Effect = "Allow",
      Action = ["codebuild:StartBuild", "codebuild:StopBuild", "codebuild:BatchGetBuilds", "codebuild:BatchGetProjects"],
      Resource = flatten([for tool in ["grype", "syft"] : [
        "arn:aws:codebuild:${var.aws_region}:${data.aws_caller_identity.current.account_id}:project/${var.name_prefix}-${tool}-scan",
        "arn:aws:codebuild:${var.aws_region}:${data.aws_caller_identity.current.account_id}:build/${var.name_prefix}-${tool}-scan:*"
      ]])
    },
    { Sid = "Identity", Effect = "Allow", Action = ["sts:GetCallerIdentity"], Resource = "*" },
    ], [for _ in range(var.security_agent_space_id == "" ? 0 : 1) : {
      Sid = "ExistingCodeReviewSpace", Effect = "Allow",
      Action = ["securityagent:UpdateAgentSpace", "securityagent:BatchGetAgentSpaces", "securityagent:CreateCodeReview", "securityagent:StartCodeReviewJob",
      "securityagent:StopCodeReviewJob", "securityagent:BatchGetCodeReviewJobs", "securityagent:ListFindings", "securityagent:BatchGetFindings"],
      Resource = "arn:aws:securityagent:${var.aws_region}:${data.aws_caller_identity.current.account_id}:agent-space/${var.security_agent_space_id}"
      }], [for _ in range(var.security_agent_space_id == "" ? 0 : 1) : {
      Sid       = "OnlyScannerServiceRole", Effect = "Allow", Action = ["iam:PassRole"],
      Resource  = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.name_prefix}-securityagent-nightly",
      Condition = { StringEquals = { "iam:PassedToService" = "securityagent.amazonaws.com" } }
  }]) })
}
output "scan_role_arn" { value = aws_iam_role.scan.arn }
