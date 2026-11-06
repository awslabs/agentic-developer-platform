variable "runner_runtime" {
  description = "Exact product runner refresh and immutable delivery inputs; null adds no access. No IAM or instance mutation is granted."
  type = object({
    repository_id    = number
    group_id         = number
    cluster_name     = string
    access_entry_arn = string
    input_versions   = map(string)
  })
  default = null
  validation {
    condition = var.runner_runtime == null ? true : (
      var.runner_runtime.repository_id > 0 && floor(var.runner_runtime.repository_id) == var.runner_runtime.repository_id &&
      var.runner_runtime.group_id > 0 && floor(var.runner_runtime.group_id) == var.runner_runtime.group_id &&
      can(regex("^[A-Za-z0-9][A-Za-z0-9_-]+$", var.runner_runtime.cluster_name)) &&
      can(regex("^arn:aws:eks:${var.aws_region}:${var.account_id}:access-entry/${var.runner_runtime.cluster_name}/role/${var.account_id}/adp-${var.environment}-gitlab-trusted-deployment/[A-Za-z0-9-]+$", var.runner_runtime.access_entry_arn)) &&
      length(var.runner_runtime.input_versions) == 2 &&
      toset([for arn in keys(var.runner_runtime.input_versions) : basename(arn)]) == toset(["tfplan", "release-lock.json"]) &&
      alltrue([for arn, version in var.runner_runtime.input_versions :
        can(regex("^arn:aws:s3:::adp-${var.environment}-deployment-evidence-${var.account_id}/gitlab-runners/inputs/[0-9a-f]{64}/(tfplan|release-lock[.]json)$", arn)) &&
        can(regex("^[A-Za-z0-9_.~%-]{1,256}$", version)) && version != "null"
      ])
    )
    error_message = "Bind the existing namespace access entry and exactly two content-addressed, versioned runner input objects."
  }
}

locals {
  runner_state_bucket = "arn:aws:s3:::adp-terraform-state-${var.account_id}"
  runner_state_key    = "${var.environment}/modules/gitlab-runners/terraform.tfstate"
  runner_cache_bucket = "arn:aws:s3:::adp-${var.environment}-gitlab-runners-${var.account_id}"
  runner_live_checks = [
    "eks-manager-ready", "same-vpc-private-asg", "qualified-image-lock", "project-ci-enabled",
    "adp-project-runner-assignment", "legal-pipeline-ran", "idle-scale-to-zero",
  ]
  runner_evidence_arns = var.runner_runtime == null ? [] : [for check in local.runner_live_checks :
    "arn:aws:s3:::adp-${var.environment}-deployment-evidence-${var.account_id}/gitlab-runners/repos/${var.runner_runtime.repository_id}/runs/*/attempts/*/${check}.json"
  ]
}

resource "aws_iam_policy" "runner_runtime_refresh" {
  count = var.runner_runtime == null ? 0 : 1
  name  = "adp-${var.environment}-gitlab-runner-runtime-refresh"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid = "ReadRegionalRunnerInventory", Effect = "Allow"
        Action = [
          "ec2:DescribeImages", "ec2:DescribeInstances", "ec2:DescribeLaunchTemplates", "ec2:DescribeLaunchTemplateVersions",
          "ec2:DescribeNetworkInterfaces", "ec2:DescribeVpcEndpoints", "ec2:DescribeSubnets", "ec2:DescribeVpcs",
          "ec2:DescribeSecurityGroups", "ec2:DescribeSecurityGroupRules", "autoscaling:DescribeAutoScalingGroups",
          "autoscaling:DescribeScalingActivities", "autoscaling:DescribeWarmPool",
        ]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
      },
      {
        Sid    = "ReadBoundRunnerRoleMetadata", Effect = "Allow"
        Action = ["iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies"]
        Resource = [
          "arn:aws:iam::${var.account_id}:role/adp-${var.environment}-gitlab-runner-${var.runner_runtime.group_id}",
          "arn:aws:iam::${var.account_id}:role/adp-${var.environment}-gitlab-runners-reconciler",
        ]
      },
      {
        Sid      = "ReadExistingNamespaceAccess", Effect = "Allow"
        Action   = ["eks:DescribeCluster", "eks:DescribeAccessEntry", "eks:ListAssociatedAccessPolicies"]
        Resource = ["arn:aws:eks:${var.aws_region}:${var.account_id}:cluster/${var.runner_runtime.cluster_name}", var.runner_runtime.access_entry_arn]
      },
      {
        Sid      = "ReadProductManagerReceipts", Effect = "Allow", Action = ["ssm:GetParameter", "ssm:ListTagsForResource"]
        Resource = [for name in ["manager-alive", "registration-receipt"] : "arn:aws:ssm:${var.aws_region}:${var.account_id}:parameter/adp/${var.environment}/gitlab-runners/${var.runner_runtime.group_id}/${name}"]
      },
      {
        Sid = "ReadRunnerCacheControls", Effect = "Allow"
        Action = [
          "s3:ListBucket", "s3:GetBucketLocation", "s3:GetBucketPolicy", "s3:GetBucketAcl", "s3:GetBucketCORS",
          "s3:GetBucketWebsite", "s3:GetBucketVersioning", "s3:GetAccelerateConfiguration", "s3:GetBucketRequestPayment",
          "s3:GetBucketLogging", "s3:GetLifecycleConfiguration", "s3:GetReplicationConfiguration", "s3:GetEncryptionConfiguration",
          "s3:GetBucketObjectLockConfiguration", "s3:GetBucketTagging", "s3:GetBucketPublicAccessBlock",
        ]
        Resource = local.runner_cache_bucket
      },
      {
        Sid       = "FindOriginalRunnerState", Effect = "Allow", Action = "s3:ListBucket", Resource = local.runner_state_bucket
        Condition = { StringEquals = { "s3:prefix" = [local.runner_state_key, "env:/"] } }
      },
      {
        Sid      = "ReadOriginalRunnerState", Effect = "Allow", Action = "s3:GetObject"
        Resource = ["${local.runner_state_bucket}/${local.runner_state_key}", "${local.runner_state_bucket}/${local.runner_state_key}.tflock", "${local.runner_state_bucket}/${var.environment}/platform/terraform.tfstate"]
      },
      {
        Sid      = "WriteOnlyRunnerStateAndLock", Effect = "Allow", Action = "s3:PutObject"
        Resource = ["${local.runner_state_bucket}/${local.runner_state_key}", "${local.runner_state_bucket}/${local.runner_state_key}.tflock"]
      },
      {
        Sid      = "ReleaseOnlyRunnerLock", Effect = "Allow", Action = "s3:DeleteObject"
        Resource = "${local.runner_state_bucket}/${local.runner_state_key}.tflock"
      },
    ]
  })
}

resource "aws_iam_policy" "runner_runtime_evidence" {
  count = var.runner_runtime == null ? 0 : 1
  name  = "adp-${var.environment}-gitlab-runner-runtime-evidence"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid = "ReadVersionedRunnerEvidence", Effect = "Allow", Action = "s3:GetObjectVersion", Resource = local.runner_evidence_arns
      },
      {
        Sid       = "WriteImmutableRunnerEvidence", Effect = "Allow", Action = "s3:PutObject", Resource = local.runner_evidence_arns
        Condition = { StringEquals = { "s3:x-amz-server-side-encryption" = "AES256", "s3:if-none-match" = "*" } }
      },
      {
        Sid       = "PublishImmutableRunnerContext", Effect = "Allow", Action = "s3:PutObject"
        Resource  = "arn:aws:s3:::adp-${var.environment}-deployment-evidence-${var.account_id}/deployment-evidence/v1/${var.runner_runtime.repository_id}/*/*/context/gitlab-runners-apply.yml.json"
        Condition = { StringEquals = { "s3:if-none-match" = "*" } }
      },
      ], [for arn, version in var.runner_runtime.input_versions : {
        Effect    = "Allow", Action = "s3:GetObjectVersion", Resource = arn
        Condition = { StringEquals = { "s3:VersionId" = version } }
    }])
  })
}

resource "aws_iam_role_policy_attachment" "runner_runtime_refresh" {
  count      = var.runner_runtime == null ? 0 : 1
  role       = aws_iam_role.deployment.name
  policy_arn = aws_iam_policy.runner_runtime_refresh[0].arn
}

resource "aws_iam_role_policy_attachment" "runner_runtime_evidence" {
  count      = var.runner_runtime == null ? 0 : 1
  role       = aws_iam_role.deployment.name
  policy_arn = aws_iam_policy.runner_runtime_evidence[0].arn
}
