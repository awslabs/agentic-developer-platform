variable "registration_refresh" {
  description = "Verified resource inventory for an ordinary GitLab Terraform refresh; null adds no access."
  type = object({
    role_arns                   = set(string)
    instance_profile_arn        = string
    instance_arn                = string
    secret_arns                 = set(string)
    parameter_arns              = set(string)
    backup_bucket_arn           = optional(string)
    cognito_user_pool_arn       = optional(string)
    hosted_zone_arn             = optional(string)
    sns_topic_arn               = optional(string)
    alarm_arn                   = optional(string)
    document_arn                = optional(string)
    maintenance_window_arn      = optional(string)
    maintenance_window_task_arn = optional(string)
  })
  default = null
  validation {
    condition = var.registration_refresh == null ? true : (
      length(var.registration_refresh.role_arns) > 0 &&
      alltrue([for arn in var.registration_refresh.role_arns : can(regex("^arn:aws:iam::${var.account_id}:role/[A-Za-z0-9+=,.@_/-]+$", arn))]) &&
      can(regex("^arn:aws:iam::${var.account_id}:instance-profile/[A-Za-z0-9+=,.@_/-]+$", var.registration_refresh.instance_profile_arn)) &&
      can(regex("^arn:aws:ec2:${var.aws_region}:${var.account_id}:instance/i-[0-9a-f]+$", var.registration_refresh.instance_arn)) &&
      length(var.registration_refresh.secret_arns) > 0 &&
      alltrue([for arn in var.registration_refresh.secret_arns : can(regex("^arn:aws:secretsmanager:${var.aws_region}:${var.account_id}:secret:adp/${var.environment}/[A-Za-z0-9/_-]+-[A-Za-z0-9]{6}$", arn))]) &&
      length(var.registration_refresh.parameter_arns) > 0 &&
      alltrue([for arn in var.registration_refresh.parameter_arns : can(regex("^arn:aws:ssm:${var.aws_region}:${var.account_id}:parameter/adp/${var.environment}/gitlab[-/][A-Za-z0-9/_-]+$", arn))]) &&
      (var.registration_refresh.backup_bucket_arn == null || var.registration_refresh.backup_bucket_arn == "arn:aws:s3:::adp-${var.environment}-gitlab-backups-${var.account_id}") &&
      (var.registration_refresh.cognito_user_pool_arn == null || can(regex("^arn:aws:cognito-idp:${var.aws_region}:${var.account_id}:userpool/[A-Za-z0-9_-]+$", var.registration_refresh.cognito_user_pool_arn))) &&
      (var.registration_refresh.hosted_zone_arn == null || can(regex("^arn:aws:route53:::hostedzone/[A-Z0-9]+$", var.registration_refresh.hosted_zone_arn))) &&
      (var.registration_refresh.sns_topic_arn == null || var.registration_refresh.sns_topic_arn == "arn:aws:sns:${var.aws_region}:${var.account_id}:adp-${var.environment}-gitlab-backup-alerts") &&
      (var.registration_refresh.alarm_arn == null || var.registration_refresh.alarm_arn == "arn:aws:cloudwatch:${var.aws_region}:${var.account_id}:alarm:adp-${var.environment}-gitlab-backup-freshness") &&
      (var.registration_refresh.document_arn == null || var.registration_refresh.document_arn == "arn:aws:ssm:${var.aws_region}:${var.account_id}:document/adp-${var.environment}-gitlab-backup") &&
      (var.registration_refresh.maintenance_window_arn == null || can(regex("^arn:aws:ssm:${var.aws_region}:${var.account_id}:maintenancewindow/mw-[0-9a-f]+$", var.registration_refresh.maintenance_window_arn))) &&
      (var.registration_refresh.maintenance_window_task_arn == null || can(regex("^arn:aws:ssm:${var.aws_region}:${var.account_id}:windowtask/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.registration_refresh.maintenance_window_task_arn)))
    )
    error_message = "Refresh must bind existing resources in this account/environment without wildcard ARNs."
  }
}

# A managed policy keeps the deployment role's cumulative inline-policy size
# below the IAM limit. No instance mutation, IAM writes, PassRole or Secrets Manager value
# reads are added. SSM GetParameter includes the existing module's OIDC parameter
# because an ordinary Terraform refresh reads it; its value remains private.
resource "aws_iam_policy" "registration_refresh" {
  count = var.registration_refresh == null ? 0 : 1
  name  = "adp-${var.environment}-gitlab-registration-refresh"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [for statement in [
      {
        Sid      = "ReadBoundRoleMetadata", Effect = "Allow"
        Action   = ["iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies"]
        Resource = sort(tolist(var.registration_refresh.role_arns))
      },
      {
        Sid      = "ReadBoundInstanceProfile", Effect = "Allow", Action = "iam:GetInstanceProfile"
        Resource = var.registration_refresh.instance_profile_arn
      },
      {
        Sid      = "ReadBoundInstanceAttributes", Effect = "Allow", Action = "ec2:DescribeInstanceAttribute"
        Resource = var.registration_refresh.instance_arn
      },
      {
        Sid = "ReadRegionalInventory", Effect = "Allow"
        # These actions have no resource types in AWS's Service Authorization
        # Reference. Enumerate APIs, and constrain them to the selected region.
        Action = [
          "ec2:DescribeImages", "ec2:DescribeInstances", "ec2:DescribeInstanceTypes",
          "ec2:DescribeVolumes", "ec2:DescribeVpcs",
          "ec2:DescribeSecurityGroups", "ec2:DescribeSecurityGroupRules",
          "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeLoadBalancerAttributes",
          "elasticloadbalancing:DescribeCapacityReservation", "elasticloadbalancing:DescribeListeners",
          "elasticloadbalancing:DescribeListenerAttributes", "elasticloadbalancing:DescribeRules",
          "elasticloadbalancing:DescribeTargetGroups", "elasticloadbalancing:DescribeTargetGroupAttributes",
          "elasticloadbalancing:DescribeTargetHealth", "elasticloadbalancing:DescribeTags", "ssm:DescribeParameters",
          "cognito-idp:ListUserPools",
        ]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
      },
      {
        Sid = "FindConfiguredPrivateZone", Effect = "Allow", Action = "route53:ListHostedZones"
        # Route 53 is global and this list API cannot bind a zone ARN.
        Resource = var.registration_refresh.hosted_zone_arn == null ? null : "*"
      },
      {
        Sid      = "ReadBoundSecretMetadata", Effect = "Allow"
        Action   = ["secretsmanager:DescribeSecret", "secretsmanager:GetResourcePolicy"]
        Resource = sort(tolist(var.registration_refresh.secret_arns))
      },
      {
        Sid      = "ReadBoundParameters", Effect = "Allow"
        Action   = ["ssm:GetParameter", "ssm:ListTagsForResource"]
        Resource = sort(tolist(var.registration_refresh.parameter_arns))
      },
      {
        Sid      = "ReadBoundBackupDocument", Effect = "Allow"
        Action   = ["ssm:GetDocument", "ssm:DescribeDocument", "ssm:DescribeDocumentPermission"]
        Resource = var.registration_refresh.document_arn
      },
      {
        Sid      = "ReadBoundMaintenanceWindow", Effect = "Allow"
        Action   = ["ssm:GetMaintenanceWindow", "ssm:DescribeMaintenanceWindowTargets", "ssm:GetMaintenanceWindowTask", "ssm:ListTagsForResource"]
        Resource = var.registration_refresh.maintenance_window_arn
      },
      {
        # Live SSM authorization also checks windowtask/<task UUID>. Retain the
        # window allow above because AWS service metadata lists maintenancewindow.
        Sid      = "ReadBoundMaintenanceWindowTask", Effect = "Allow"
        Action   = "ssm:GetMaintenanceWindowTask"
        Resource = var.registration_refresh.maintenance_window_task_arn
      },
      {
        Sid = "ReadBoundBackupBucketControls", Effect = "Allow"
        Action = [
          "s3:ListBucket", "s3:GetBucketLocation", "s3:GetBucketPolicy", "s3:GetBucketAcl", "s3:GetBucketCORS",
          "s3:GetBucketWebsite", "s3:GetBucketVersioning", "s3:GetAccelerateConfiguration", "s3:GetBucketRequestPayment",
          "s3:GetBucketLogging", "s3:GetLifecycleConfiguration", "s3:GetReplicationConfiguration", "s3:GetEncryptionConfiguration",
          "s3:GetBucketObjectLockConfiguration", "s3:GetBucketTagging", "s3:GetBucketPublicAccessBlock",
        ]
        Resource = var.registration_refresh.backup_bucket_arn
      },
      {
        Sid      = "ReadBoundCognitoClient", Effect = "Allow", Action = "cognito-idp:DescribeUserPoolClient"
        Resource = var.registration_refresh.cognito_user_pool_arn
      },
      {
        Sid      = "ReadBoundDnsRecords", Effect = "Allow", Action = ["route53:GetHostedZone", "route53:ListResourceRecordSets", "route53:ListTagsForResource"]
        Resource = var.registration_refresh.hosted_zone_arn
      },
      {
        Sid      = "ReadBoundBackupTopic", Effect = "Allow", Action = ["sns:GetTopicAttributes", "sns:ListTagsForResource"]
        Resource = var.registration_refresh.sns_topic_arn
      },
      {
        Sid      = "ReadBoundBackupAlarm", Effect = "Allow", Action = ["cloudwatch:DescribeAlarms", "cloudwatch:ListTagsForResource"]
        Resource = var.registration_refresh.alarm_arn
      },
    ] : statement if statement.Resource != null]
  })
}

resource "aws_iam_role_policy_attachment" "registration_refresh" {
  count      = var.registration_refresh == null ? 0 : 1
  role       = aws_iam_role.deployment.name
  policy_arn = aws_iam_policy.registration_refresh[0].arn
}
