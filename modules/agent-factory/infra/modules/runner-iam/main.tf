# =============================================================================
# Runner IAM — IRSA role for GitHub Actions runner pods
# =============================================================================
# Extracted from github-actions-runner/infrastructure/iam.tf
# Uses the shared EKS OIDC provider instead of creating a new one.
#
# Policy split rationale (Issue #1204):
# The scoped runner policy from synthesis #1200 is ~11 KB, exceeding the AWS
# managed-policy limit of 10,240 bytes. Split into two managed policies:
#   - runner-base: infrastructure-heavy statements (EC2, EKS, IAM, KMS,
#     CloudTrail, EventBridge, CodeBuild, ECR, ELB)
#   - runner-services: application-deploy statements (S3, SecretsManager, SSM,
#     CloudFront, Lambda, SQS, DynamoDB, Logs, WAFv2, APIGateway, STS, Bedrock,
#     ExecuteAPI, CloudWatch)
# =============================================================================

data "aws_caller_identity" "current" {}

# Permissions boundary — scoped version (Issue #1204, #596 fix)
resource "aws_iam_policy" "runner_boundary" {
  name        = "${var.name_prefix}-runner-boundary"
  description = "Permissions boundary for GitHub runner pods"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowBroadAccess"
        Effect = "Allow"
        Action = [
          "ec2:*", "s3:*", "lambda:*", "dynamodb:*", "rds:*",
          "ecs:*", "ecr:*", "elasticloadbalancing:*", "autoscaling:*",
          "cloudformation:*", "cloudwatch:*", "logs:*", "sns:*", "sqs:*",
          "apigateway:*", "route53:*", "cloudfront:*", "acm:*",
          "amplify:*", "codebuild:*",
          "secretsmanager:*",
          "ssm:*",

          # KMS — encrypt/decrypt for data, key management for Terraform
          # create/destroy, plus wildcarded reads for Terraform refresh.
          "kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*",
          "kms:Describe*", "kms:Get*", "kms:List*",
          "kms:CreateKey", "kms:CreateGrant", "kms:RetireGrant",
          "kms:TagResource", "kms:UntagResource",
          "kms:ScheduleKeyDeletion", "kms:PutKeyPolicy",
          "kms:EnableKeyRotation", "kms:DisableKeyRotation",
          "kms:CreateAlias", "kms:DeleteAlias", "kms:UpdateAlias",

          # IAM — broad read-only via wildcards.
          "iam:Get*", "iam:List*", "iam:Simulate*", "iam:Generate*",

          # IAM writes — scoped to what Terraform apply actually uses.
          "iam:CreateRole", "iam:CreatePolicy", "iam:AttachRolePolicy",
          "iam:PutRolePolicy", "iam:PassRole", "iam:TagRole", "iam:TagPolicy",
          "iam:CreateServiceLinkedRole",
          "iam:DeleteRole", "iam:DeleteRolePolicy", "iam:DetachRolePolicy",
          # Issue #596: iam:DeletePolicy + DeletePolicyVersion required for
          # Terraform to replace inline policies with managed policies.
          "iam:DeletePolicy", "iam:DeletePolicyVersion",
          "iam:CreatePolicyVersion", "iam:DeletePolicyVersion",
          "iam:SetDefaultPolicyVersion",
          "iam:UpdateAssumeRolePolicy",
          "iam:CreateInstanceProfile", "iam:AddRoleToInstanceProfile",
          "iam:DeleteInstanceProfile", "iam:RemoveRoleFromInstanceProfile",
          "iam:TagInstanceProfile",
          "iam:CreateOpenIDConnectProvider", "iam:DeleteOpenIDConnectProvider",
          "iam:AddClientIDToOpenIDConnectProvider",
          "iam:TagOpenIDConnectProvider", "iam:UntagOpenIDConnectProvider",
          "iam:UntagRole", "iam:UpdateRole",
          "sts:AssumeRole", "sts:GetCallerIdentity",
          "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
          "events:*", "stepfunctions:*",
          "cognito-idp:*", "cognito-identity:*",
          "elasticache:*", "eks:*",
          "sagemaker:*",
          # WAFv2 — needed by webhook-ingress module to protect API Gateway
          "wafv2:CreateWebACL", "wafv2:DeleteWebACL", "wafv2:UpdateWebACL",
          "wafv2:GetWebACL", "wafv2:ListWebACLs",
          "wafv2:AssociateWebACL", "wafv2:DisassociateWebACL",
          "wafv2:GetWebACLForResource", "wafv2:ListResourcesForWebACL",
          "wafv2:TagResource", "wafv2:UntagResource", "wafv2:ListTagsForResource",
          # CloudTrail
          "cloudtrail:*"
        ]
        Resource = "*"
      },
      {
        # Self-manage boundary versions — required for Terraform to update the
        # boundary from a runner pod.
        Sid    = "ManageOwnBoundary"
        Effect = "Allow"
        Action = [
          "iam:CreatePolicyVersion",
          "iam:DeletePolicyVersion",
          "iam:SetDefaultPolicyVersion",
          "iam:ListPolicyVersions",
          "iam:GetPolicyVersion"
        ]
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/${var.name_prefix}-runner-boundary"
      },
      {
        Sid      = "ExecuteApi"
        Effect   = "Allow"
        Action   = ["execute-api:*"]
        Resource = "*"
      },
      {
        Sid    = "DenyDangerousActions"
        Effect = "Deny"
        Action = [
          "iam:CreateUser", "iam:DeleteUser",
          "iam:CreateLoginProfile", "iam:UpdateLoginProfile",
          "iam:CreateAccessKey", "iam:UpdateAccessKey",
          "organizations:*", "account:*",
          "aws-portal:*", "billing:*",
          "budgets:ModifyBudget", "budgets:DeleteBudget", "ce:*"
        ]
        Resource = "*"
      }
    ]
  })
}

# IRSA role for runner pods
resource "aws_iam_role" "runner" {
  name                 = "${var.name_prefix}-runner-role"
  permissions_boundary = aws_iam_policy.runner_boundary.arn

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = var.oidc_provider_arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringLike = {
          "${replace(var.oidc_issuer, "https://", "")}:sub" = "system:serviceaccount:${var.runner_namespace}*:github-runner-sa"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "runner_security_scan_upload" {
  count = var.security_scans_bucket_arn != "" ? 1 : 0

  name = "security-scan-s3-upload"
  role = aws_iam_role.runner.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "SecurityScanSARIFUpload"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${var.security_scans_bucket_arn}/sarif/*"
      }
    ]
  })
}

# =============================================================================
# Runner Managed Policies (Issue #1204 — synthesis #1200 Section 1)
# =============================================================================
# Split into two policies to stay under the 10,240-byte AWS managed-policy
# limit. Policy A = infrastructure-heavy, Policy B = application-deploy.
# Both are attached to the same runner role.
# =============================================================================

resource "aws_iam_policy" "runner_base" {
  name        = "${var.name_prefix}-runner-base"
  description = "Runner scoped policy — infrastructure (EC2, EKS, IAM, KMS, CloudTrail, EventBridge, CodeBuild, ECR, ELB)"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "APIGatewayMgmt"
        Effect = "Allow"
        Action = [
          "apigateway:*"
        ]
        Resource = "arn:aws:apigateway:us-east-1::/*"
      },
      {
        Sid    = "CloudTrailMgmt"
        Effect = "Allow"
        Action = [
          "cloudtrail:AddTags",
          "cloudtrail:CreateTrail",
          "cloudtrail:DeleteTrail",
          "cloudtrail:DescribeTrails",
          "cloudtrail:GetInsightSelectors",
          "cloudtrail:GetTrail",
          "cloudtrail:GetTrailStatus",
          "cloudtrail:ListTags",
          "cloudtrail:PutInsightSelectors",
          "cloudtrail:RemoveTags",
          "cloudtrail:StartLogging",
          "cloudtrail:StopLogging",
          "cloudtrail:UpdateTrail"
        ]
        Resource = "arn:aws:cloudtrail:us-east-1:*:trail/adp-*-trail"
      },
      {
        Sid    = "CodeBuildProjects"
        Effect = "Allow"
        Action = [
          "codebuild:BatchGetBuilds",
          "codebuild:BatchGetProjects",
          "codebuild:CreateProject",
          "codebuild:DeleteProject",
          "codebuild:ListProjects",
          "codebuild:StartBuild",
          "codebuild:UpdateProject"
        ]
        Resource = "arn:aws:codebuild:us-east-1:*:project/adp-*"
      },
      {
        Sid    = "EC2VPCAndNetworking"
        Effect = "Allow"
        Action = [
          "ec2:AllocateAddress",
          "ec2:AssociateRouteTable",
          "ec2:AttachInternetGateway",
          "ec2:AuthorizeSecurityGroupEgress",
          "ec2:AuthorizeSecurityGroupIngress",
          "ec2:CreateInternetGateway",
          "ec2:CreateNatGateway",
          "ec2:CreateRoute",
          "ec2:CreateRouteTable",
          "ec2:CreateSecurityGroup",
          "ec2:CreateSubnet",
          "ec2:CreateTags",
          "ec2:CreateVpc",
          "ec2:CreateVpcEndpoint",
          "ec2:DeleteInternetGateway",
          "ec2:DeleteNatGateway",
          "ec2:DeleteNetworkInterface",
          "ec2:DeleteRoute",
          "ec2:DeleteRouteTable",
          "ec2:DeleteSecurityGroup",
          "ec2:DeleteSubnet",
          "ec2:DeleteTags",
          "ec2:DeleteVpc",
          "ec2:DeleteVpcEndpoints",
          "ec2:DescribeAccountAttributes",
          "ec2:DescribeAddresses",
          "ec2:DescribeAvailabilityZones",
          "ec2:DescribeInternetGateways",
          "ec2:DescribeNatGateways",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DescribePrefixLists",
          "ec2:DescribeRouteTables",
          "ec2:DescribeSecurityGroupRules",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeSubnets",
          "ec2:DescribeTags",
          "ec2:DescribeVpcEndpoints",
          "ec2:DescribeVpcs",
          "ec2:DetachInternetGateway",
          "ec2:DisassociateRouteTable",
          "ec2:ModifySubnetAttribute",
          "ec2:ModifyVpcAttribute",
          "ec2:ModifyVpcEndpoint",
          "ec2:ReleaseAddress",
          "ec2:RevokeSecurityGroupEgress",
          "ec2:RevokeSecurityGroupIngress"
        ]
        Resource = "*"
      },
      {
        Sid    = "ECRRegistryAndRepo"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:CompleteLayerUpload",
          "ecr:CreatePullThroughCacheRule",
          "ecr:CreateRepository",
          "ecr:DeleteLifecyclePolicy",
          "ecr:DeletePullThroughCacheRule",
          "ecr:DeleteRepository",
          "ecr:DeleteRepositoryPolicy",
          "ecr:DescribePullThroughCacheRules",
          "ecr:DescribeRepositories",
          "ecr:GetAuthorizationToken",
          "ecr:GetDownloadUrlForLayer",
          "ecr:GetLifecyclePolicy",
          "ecr:GetRegistryScanningConfiguration",
          "ecr:GetRepositoryPolicy",
          "ecr:InitiateLayerUpload",
          "ecr:ListImages",
          "ecr:ListTagsForResource",
          "ecr:PutImage",
          "ecr:PutLifecyclePolicy",
          "ecr:PutRegistryScanningConfiguration",
          "ecr:SetRepositoryPolicy",
          "ecr:TagResource",
          "ecr:UntagResource",
          "ecr:UploadLayerPart"
        ]
        Resource = [
          "*",
          "arn:aws:ecr:us-east-1:*:repository/adp-*"
        ]
      },
      {
        Sid    = "EKSClusterOps"
        Effect = "Allow"
        Action = [
          "eks:AccessKubernetesApi",
          "eks:AssociateAccessPolicy",
          "eks:CreateAccessEntry",
          "eks:CreateAddon",
          "eks:CreateCluster",
          "eks:DeleteAccessEntry",
          "eks:DeleteAddon",
          "eks:DeleteCluster",
          "eks:DescribeAccessEntry",
          "eks:DescribeAddon",
          "eks:DescribeCluster",
          "eks:DescribeNodegroup",
          "eks:DisassociateAccessPolicy",
          "eks:ListAccessEntries",
          "eks:ListAddons",
          "eks:ListAssociatedAccessPolicies",
          "eks:ListClusters",
          "eks:ListNodegroups",
          "eks:TagResource",
          "eks:UntagResource",
          "eks:UpdateAddon",
          "eks:UpdateClusterConfig",
          "eks:UpdateClusterVersion"
        ]
        Resource = [
          "arn:aws:eks:*:*:cluster/adp-*-eks*",
          "arn:aws:eks:us-east-1:*:access-entry/adp-*-eks-cluster/*",
          "arn:aws:eks:us-east-1:*:addon/adp-*-eks-cluster/*/*"
        ]
      },
      {
        Sid    = "ELBDiscovery"
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:DescribeLoadBalancers"
        ]
        Resource = "*"
      },
      {
        Sid    = "EventBridgeRules"
        Effect = "Allow"
        Action = [
          "events:DeleteRule",
          "events:DescribeRule",
          "events:ListTagsForResource",
          "events:ListTargetsByRule",
          "events:PutRule",
          "events:PutTargets",
          "events:RemoveTargets",
          "events:TagResource",
          "events:UntagResource"
        ]
        Resource = "arn:aws:events:us-east-1:*:rule/*-ecr-image-push"
      },
      {
        Sid    = "IAMRolePolicyMgmt"
        Effect = "Allow"
        Action = [
          "iam:AddClientIDToOpenIDConnectProvider",
          "iam:AddRoleToInstanceProfile",
          "iam:AttachRolePolicy",
          "iam:CreateInstanceProfile",
          "iam:CreateOpenIDConnectProvider",
          "iam:CreatePolicy",
          "iam:CreatePolicyVersion",
          "iam:CreateRole",
          "iam:CreateServiceLinkedRole",
          "iam:DeleteInstanceProfile",
          "iam:DeleteOpenIDConnectProvider",
          "iam:DeletePolicy",
          "iam:DeletePolicyVersion",
          "iam:DeleteRole",
          "iam:DeleteRolePolicy",
          "iam:DetachRolePolicy",
          "iam:GetInstanceProfile",
          "iam:GetOpenIDConnectProvider",
          "iam:GetPolicy",
          "iam:GetRole",
          "iam:GetRolePolicy",
          "iam:ListAttachedRolePolicies",
          "iam:ListInstanceProfilesForRole",
          "iam:ListPolicies",
          "iam:ListRolePolicies",
          "iam:ListRoleTags",
          "iam:ListRoles",
          "iam:PassRole",
          "iam:PutRolePolicy",
          "iam:RemoveRoleFromInstanceProfile",
          "iam:SetDefaultPolicyVersion",
          "iam:TagInstanceProfile",
          "iam:TagOpenIDConnectProvider",
          "iam:TagPolicy",
          "iam:TagRole",
          "iam:UntagOpenIDConnectProvider",
          "iam:UntagRole",
          "iam:UpdateAssumeRolePolicy",
          "iam:UpdateRole"
        ]
        Resource = [
          "*",
          "arn:aws:iam::*:instance-profile/adp-*",
          "arn:aws:iam::*:oidc-provider/oidc.eks.*.amazonaws.com/*",
          "arn:aws:iam::*:policy/adp-*",
          "arn:aws:iam::*:role/adp-*"
        ]
      },
      {
        Sid    = "KMSKeyLifecycle"
        Effect = "Allow"
        Action = [
          "kms:CreateAlias",
          "kms:CreateGrant",
          "kms:CreateKey",
          "kms:Decrypt",
          "kms:DeleteAlias",
          "kms:Describe*",
          "kms:DisableKeyRotation",
          "kms:EnableKeyRotation",
          "kms:Encrypt",
          "kms:GenerateDataKey*",
          "kms:Get*",
          "kms:List*",
          "kms:PutKeyPolicy",
          "kms:RetireGrant",
          "kms:ScheduleKeyDeletion",
          "kms:TagResource",
          "kms:UntagResource",
          "kms:UpdateAlias"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_policy" "runner_services" {
  name        = "${var.name_prefix}-runner-services"
  description = "Runner scoped policy — application deploy (S3, Secrets, SSM, CloudFront, Lambda, SQS, DynamoDB, Logs, WAF, STS, Bedrock, CloudWatch)"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "BedrockModelInvoke"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream"
        ]
        Resource = [
          "arn:aws:bedrock:*:*:inference-profile/*",
          "arn:aws:bedrock:*::foundation-model/anthropic.*"
        ]
      },
      {
        Sid    = "CloudFrontDistribution"
        Effect = "Allow"
        Action = [
          "cloudfront:CreateInvalidation",
          "cloudfront:GetDistribution",
          "cloudfront:GetDistributionConfig",
          "cloudfront:UpdateDistribution"
        ]
        Resource = "arn:aws:cloudfront::*:distribution/*"
      },
      {
        Sid    = "CloudWatchMetrics"
        Effect = "Allow"
        Action = [
          "cloudwatch:DeleteAlarms",
          "cloudwatch:DeleteDashboards",
          "cloudwatch:DescribeAlarms",
          "cloudwatch:GetDashboard",
          "cloudwatch:ListDashboards",
          "cloudwatch:PutDashboard",
          "cloudwatch:PutMetricAlarm",
          "cloudwatch:PutMetricData",
          "cloudwatch:TagResource",
          "cloudwatch:UntagResource"
        ]
        Resource = "*"
      },
      {
        Sid    = "CloudWatchLogGroups"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:DeleteLogGroup",
          "logs:DescribeLogGroups",
          "logs:ListTagsForResource",
          "logs:ListTagsLogGroup",
          "logs:PutRetentionPolicy",
          "logs:TagLogGroup",
          "logs:TagResource",
          "logs:UntagLogGroup",
          "logs:UntagResource"
        ]
        Resource = [
          "arn:aws:logs:us-east-1:*:log-group:/adp/*",
          "arn:aws:logs:us-east-1:*:log-group:/aws/ecr/adp-*",
          "arn:aws:logs:us-east-1:*:log-group:/aws/eks/adp-*:*",
          "arn:aws:logs:us-east-1:*:log-group:/github-ccsdk-agent/*"
        ]
      },
      {
        Sid    = "DynamoDBTableMgmt"
        Effect = "Allow"
        Action = [
          "dynamodb:CreateTable",
          "dynamodb:DeleteItem",
          "dynamodb:DeleteTable",
          "dynamodb:DescribeContinuousBackups",
          "dynamodb:DescribeTable",
          "dynamodb:DescribeTimeToLive",
          "dynamodb:GetItem",
          "dynamodb:ListTables",
          "dynamodb:ListTagsOfResource",
          "dynamodb:PutItem",
          "dynamodb:TagResource",
          "dynamodb:UntagResource",
          "dynamodb:UpdateContinuousBackups",
          "dynamodb:UpdateTable",
          "dynamodb:UpdateTimeToLive"
        ]
        Resource = "arn:aws:dynamodb:us-east-1:*:table/adp-*"
      },
      {
        Sid    = "ExecuteAPIInvoke"
        Effect = "Allow"
        Action = [
          "execute-api:Invoke"
        ]
        Resource = [
          "arn:aws:execute-api:us-east-1:*:*/*/*/agent/*",
          "arn:aws:execute-api:us-east-1:*:*/*/*/internal/*"
        ]
      },
      {
        Sid    = "LambdaFunctions"
        Effect = "Allow"
        Action = [
          "lambda:AddPermission",
          "lambda:CreateEventSourceMapping",
          "lambda:CreateFunction",
          "lambda:DeleteEventSourceMapping",
          "lambda:DeleteFunction",
          "lambda:GetEventSourceMapping",
          "lambda:GetFunction",
          "lambda:GetFunctionConfiguration",
          "lambda:GetPolicy",
          "lambda:ListFunctions",
          "lambda:PublishVersion",
          "lambda:RemovePermission",
          "lambda:TagResource",
          "lambda:UntagResource",
          "lambda:UpdateEventSourceMapping",
          "lambda:UpdateFunctionCode",
          "lambda:UpdateFunctionConfiguration"
        ]
        Resource = "arn:aws:lambda:us-east-1:*:function:adp-*"
      },
      {
        Sid    = "S3Combined"
        Effect = "Allow"
        Action = [
          "s3:CreateBucket",
          "s3:DeleteBucket",
          "s3:DeleteBucketPolicy",
          "s3:DeleteObject",
          "s3:DeleteObjectVersion",
          "s3:GetBucketLocation",
          "s3:GetBucketPolicy",
          "s3:GetBucketPublicAccessBlock",
          "s3:GetBucketTagging",
          "s3:GetBucketVersioning",
          "s3:GetEncryptionConfiguration",
          "s3:GetLifecycleConfiguration",
          "s3:GetObject",
          "s3:GetPublicAccessBlock",
          "s3:HeadObject",
          "s3:ListAllMyBuckets",
          "s3:ListBucket",
          "s3:ListBucketVersions",
          "s3:ListObjectVersions",
          "s3:PutBucketPolicy",
          "s3:PutBucketPublicAccessBlock",
          "s3:PutBucketTagging",
          "s3:PutBucketVersioning",
          "s3:PutEncryptionConfiguration",
          "s3:PutLifecycleConfiguration",
          "s3:PutObject",
          "s3:PutPublicAccessBlock"
        ]
        Resource = [
          "arn:aws:s3:::adp-*",
          "arn:aws:s3:::adp-*/*",
          "arn:aws:s3:::bedrockgw-*",
          "arn:aws:s3:::bedrockgw-*-frontend-*",
          "arn:aws:s3:::bedrockgw-*-frontend-*/*",
          "arn:aws:s3:::bedrockgw-*/*"
        ]
      },
      {
        Sid    = "SecretsManagerOps"
        Effect = "Allow"
        Action = [
          "secretsmanager:CreateSecret",
          "secretsmanager:DeleteSecret",
          "secretsmanager:DescribeSecret",
          "secretsmanager:GetSecretValue",
          "secretsmanager:ListSecrets",
          "secretsmanager:PutSecretValue",
          "secretsmanager:RestoreSecret",
          "secretsmanager:TagResource",
          "secretsmanager:UntagResource",
          "secretsmanager:UpdateSecret"
        ]
        Resource = [
          "arn:aws:secretsmanager:*:*:secret:bedrockgw-*",
          "arn:aws:secretsmanager:us-east-1:*:secret:adp/*"
        ]
      },
      {
        Sid    = "SQSQueueMgmt"
        Effect = "Allow"
        Action = [
          "sqs:CreateQueue",
          "sqs:DeleteQueue",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
          "sqs:ListQueueTags",
          "sqs:ListQueues",
          "sqs:SetQueueAttributes",
          "sqs:TagQueue",
          "sqs:UntagQueue"
        ]
        Resource = "arn:aws:sqs:us-east-1:*:adp-*"
      },
      {
        Sid    = "SSMParameterOps"
        Effect = "Allow"
        Action = [
          "ssm:AddTagsToResource",
          "ssm:DeleteParameter",
          "ssm:DescribeParameters",
          "ssm:GetParameter",
          "ssm:GetParameters",
          "ssm:ListTagsForResource",
          "ssm:PutParameter",
          "ssm:RemoveTagsFromResource"
        ]
        Resource = "arn:aws:ssm:us-east-1:*:parameter/adp/*"
      },
      {
        Sid    = "STSIdentityAndAssume"
        Effect = "Allow"
        Action = [
          "sts:AssumeRole",
          "sts:GetCallerIdentity"
        ]
        Resource = "*"
      },
      {
        Sid    = "WAFv2WebACL"
        Effect = "Allow"
        Action = [
          "wafv2:AssociateWebACL",
          "wafv2:CreateWebACL",
          "wafv2:DeleteWebACL",
          "wafv2:DisassociateWebACL",
          "wafv2:GetWebACL",
          "wafv2:GetWebACLForResource",
          "wafv2:ListResourcesForWebACL",
          "wafv2:ListTagsForResource",
          "wafv2:ListWebACLs",
          "wafv2:TagResource",
          "wafv2:UntagResource",
          "wafv2:UpdateWebACL"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "runner_base" {
  role       = aws_iam_role.runner.name
  policy_arn = aws_iam_policy.runner_base.arn
}

resource "aws_iam_role_policy_attachment" "runner_services" {
  role       = aws_iam_role.runner.name
  policy_arn = aws_iam_policy.runner_services.arn
}
