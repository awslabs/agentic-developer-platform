variable "runner_activation" {
  description = "Existing group and cluster bindings for protected manager activation; null grants nothing."
  type = object({
    group_id              = number
    cluster_name          = string
    access_entry_arn      = string
    eks_oidc_provider_arn = string
    vpc_id                = string
  })
  default = null
  validation {
    condition = var.runner_activation == null ? true : (
      var.runner_activation.group_id > 0 && floor(var.runner_activation.group_id) == var.runner_activation.group_id &&
      var.runner_activation.cluster_name == "adp-${var.environment}-eks-cluster" &&
      can(regex("^arn:aws:eks:${var.aws_region}:${var.account_id}:access-entry/adp-${var.environment}-eks-cluster/role/${var.account_id}/adp-${var.environment}-gitlab-trusted-deployment/[A-Za-z0-9-]+$", var.runner_activation.access_entry_arn)) &&
      can(regex("^arn:aws:iam::${var.account_id}:oidc-provider/oidc[.]eks[.]${var.aws_region}[.]amazonaws[.]com/id/[A-Z0-9]+$", var.runner_activation.eks_oidc_provider_arn)) &&
      can(regex("^vpc-[0-9a-f]{8,17}$", var.runner_activation.vpc_id))
    )
    error_message = "Bind an existing group, exact protected deployment access entry, EKS OIDC provider and VPC in this account."
  }
}

locals {
  runner_activation_state_bucket = "arn:aws:s3:::adp-terraform-state-${var.account_id}"
  runner_activation_state_key    = "${var.environment}/modules/gitlab-runners/terraform.tfstate"
  runner_activation_cache_bucket = "arn:aws:s3:::adp-${var.environment}-gitlab-runners-${var.account_id}"
}

# These permissions only persist the module's state and read existing foundation
# resources. Kubernetes activation remains limited by the separate EKS entry.
resource "aws_iam_policy" "runner_activation" {
  count = var.runner_activation == null ? 0 : 1
  name  = "adp-${var.environment}-gitlab-runner-activation"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ListRunnerState", Effect = "Allow", Action = "s3:ListBucket", Resource = local.runner_activation_state_bucket
        Condition = { StringEquals = { "s3:prefix" = [local.runner_activation_state_key, "env:/"] } }
      },
      {
        Sid      = "PersistRunnerState", Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"]
        Resource = "${local.runner_activation_state_bucket}/${local.runner_activation_state_key}"
      },
      {
        Sid      = "UseRunnerStateLock", Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${local.runner_activation_state_bucket}/${local.runner_activation_state_key}.tflock"
      },
      {
        Sid    = "ReadRunnerRoles", Effect = "Allow"
        Action = ["iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies"]
        Resource = [
          "arn:aws:iam::${var.account_id}:role/adp-${var.environment}-gitlab-runner-${var.runner_activation.group_id}",
          "arn:aws:iam::${var.account_id}:role/adp-${var.environment}-gitlab-runners-reconciler",
        ]
      },
      {
        Sid = "FindExistingOidcProviders", Effect = "Allow", Action = "iam:ListOpenIDConnectProviders", Resource = "*"
      },
      {
        Sid      = "ReadExistingOidcProviders", Effect = "Allow", Action = "iam:GetOpenIDConnectProvider"
        Resource = [var.runner_activation.eks_oidc_provider_arn, "arn:aws:iam::${var.account_id}:oidc-provider/token.actions.githubusercontent.com"]
      },
      {
        Sid      = "ReadRunnerCluster", Effect = "Allow", Action = ["eks:DescribeCluster", "eks:ListTagsForResource"]
        Resource = "arn:aws:eks:${var.aws_region}:${var.account_id}:cluster/${var.runner_activation.cluster_name}"
      },
      {
        Sid      = "ReadExistingNamespaceAccess", Effect = "Allow", Action = ["eks:DescribeAccessEntry", "eks:ListAssociatedAccessPolicies", "eks:ListTagsForResource"]
        Resource = var.runner_activation.access_entry_arn
      },
      {
        Sid      = "ReadRunnerVpcAttributes", Effect = "Allow", Action = "ec2:DescribeVpcAttribute"
        Resource = "arn:aws:ec2:${var.aws_region}:${var.account_id}:vpc/${var.runner_activation.vpc_id}"
      },
      {
        Sid      = "ReadRegionalRunnerFoundation", Effect = "Allow"
        Action   = ["autoscaling:DescribeAutoScalingGroups", "autoscaling:DescribeScalingActivities", "ec2:DescribeLaunchTemplateVersions", "ec2:DescribeLaunchTemplates", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets", "ec2:DescribeRouteTables", "ec2:DescribeVpcEndpoints", "ec2:DescribeVpcEndpointServices", "ec2:DescribePrefixLists"]
        Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
      },
      {
        Sid      = "ReadRunnerManagerReceipts", Effect = "Allow", Action = ["ssm:GetParameter", "ssm:ListTagsForResource"]
        Resource = [for name in ["manager-alive", "registration-receipt"] : "arn:aws:ssm:${var.aws_region}:${var.account_id}:parameter/adp/${var.environment}/gitlab-runners/${var.runner_activation.group_id}/${name}"]
      },
      {
        Sid      = "ReadRunnerCacheControls", Effect = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation", "s3:GetBucketPolicy", "s3:GetBucketAcl", "s3:GetBucketCORS", "s3:GetBucketWebsite", "s3:GetBucketVersioning", "s3:GetAccelerateConfiguration", "s3:GetBucketRequestPayment", "s3:GetBucketLogging", "s3:GetLifecycleConfiguration", "s3:GetReplicationConfiguration", "s3:GetEncryptionConfiguration", "s3:GetBucketObjectLockConfiguration", "s3:GetBucketTagging", "s3:GetBucketPublicAccessBlock"]
        Resource = local.runner_activation_cache_bucket
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "runner_activation" {
  count      = var.runner_activation == null ? 0 : 1
  role       = aws_iam_role.deployment.name
  policy_arn = aws_iam_policy.runner_activation[0].arn
}

variable "runner_evidence" {
  description = "Exact protected repository and two version-pinned activation inputs."
  type        = object({ repository_id = number, input_versions = map(string) })
  default     = null
  validation {
    condition = var.runner_evidence == null ? true : (
      var.runner_evidence.repository_id > 0 && floor(var.runner_evidence.repository_id) == var.runner_evidence.repository_id &&
      length(var.runner_evidence.input_versions) == 2 &&
      toset([for arn in keys(var.runner_evidence.input_versions) : basename(arn)]) == toset(["tfplan", "release-lock.json"]) &&
      alltrue([for arn, version in var.runner_evidence.input_versions :
        can(regex("^arn:aws:s3:::adp-${var.environment}-deployment-evidence-${var.account_id}/gitlab-runners/inputs/[0-9a-f]{64}/(tfplan|release-lock[.]json)$", arn)) &&
        can(regex("^[A-Za-z0-9_.~%-]{1,256}$", version)) && version != "null"
      ])
    )
    error_message = "Bind one repository and exactly two content-addressed, version-pinned runner inputs."
  }
}

locals {
  runner_activation_evidence_arns = var.runner_evidence == null ? [] : [for check in ["eks-manager-ready", "same-vpc-private-asg", "qualified-image-lock", "project-ci-enabled", "adp-project-runner-assignment", "legal-pipeline-ran", "idle-scale-to-zero"] :
    "arn:aws:s3:::adp-${var.environment}-deployment-evidence-${var.account_id}/gitlab-runners/repos/${var.runner_evidence.repository_id}/runs/*/attempts/*/${check}.json"
  ]
}

resource "aws_iam_policy" "runner_evidence" {
  count = var.runner_evidence == null ? 0 : 1
  name  = "adp-${var.environment}-gitlab-runner-evidence"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([for arn, version in var.runner_evidence.input_versions : {
      Effect    = "Allow", Action = "s3:GetObjectVersion", Resource = arn
      Condition = { StringEquals = { "s3:VersionId" = version } }
      }], [
      {
        Sid = "ReadVersionedRunnerEvidence", Effect = "Allow", Action = "s3:GetObjectVersion", Resource = local.runner_activation_evidence_arns
      },
      {
        Sid       = "WriteImmutableRunnerEvidence", Effect = "Allow", Action = "s3:PutObject", Resource = local.runner_activation_evidence_arns
        Condition = { StringEquals = { "s3:if-none-match" = "*", "s3:x-amz-server-side-encryption" = "AES256" } }
      },
      {
        Sid       = "PublishImmutableRunnerContext", Effect = "Allow", Action = "s3:PutObject"
        Resource  = "arn:aws:s3:::adp-${var.environment}-deployment-evidence-${var.account_id}/deployment-evidence/v1/${var.runner_evidence.repository_id}/*/*/context/gitlab-runners-apply.yml.json"
        Condition = { StringEquals = { "s3:if-none-match" = "*" } }
      },
      {
        Sid      = "ReadVersionedRunnerDiagnostic", Effect = "Allow", Action = "s3:GetObjectVersion"
        Resource = "arn:aws:s3:::adp-${var.environment}-deployment-evidence-${var.account_id}/gitlab-runner-diagnostics/repos/${var.runner_evidence.repository_id}/runs/*/attempts/*/terraform-failure.json"
      },
      {
        Sid       = "WriteImmutableRunnerDiagnostic", Effect = "Allow", Action = "s3:PutObject"
        Resource  = "arn:aws:s3:::adp-${var.environment}-deployment-evidence-${var.account_id}/gitlab-runner-diagnostics/repos/${var.runner_evidence.repository_id}/runs/*/attempts/*/terraform-failure.json"
        Condition = { StringEquals = { "s3:if-none-match" = "*", "s3:x-amz-server-side-encryption" = "AES256" } }
      },
    ])
  })
}

resource "aws_iam_role_policy_attachment" "runner_evidence" {
  count      = var.runner_evidence == null ? 0 : 1
  role       = aws_iam_role.deployment.name
  policy_arn = aws_iam_policy.runner_evidence[0].arn
}
