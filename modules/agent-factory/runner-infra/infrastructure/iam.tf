# =============================================================================
# EKS Cluster IAM Role
# =============================================================================

resource "aws_iam_role" "cluster" {
  name = "${local.cluster_name}-cluster-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = ["sts:AssumeRole", "sts:TagSession"]
      Effect = "Allow"
      Principal = {
        Service = "eks.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "cluster_AmazonEKSClusterPolicy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
  role       = aws_iam_role.cluster.name
}

resource "aws_iam_role_policy_attachment" "cluster_AmazonEKSComputePolicy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSComputePolicy"
  role       = aws_iam_role.cluster.name
}

resource "aws_iam_role_policy_attachment" "cluster_AmazonEKSBlockStoragePolicy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSBlockStoragePolicy"
  role       = aws_iam_role.cluster.name
}

resource "aws_iam_role_policy_attachment" "cluster_AmazonEKSLoadBalancingPolicy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSLoadBalancingPolicy"
  role       = aws_iam_role.cluster.name
}

resource "aws_iam_role_policy_attachment" "cluster_AmazonEKSNetworkingPolicy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSNetworkingPolicy"
  role       = aws_iam_role.cluster.name
}

resource "aws_iam_role_policy_attachment" "cluster_AmazonEKSVPCResourceController" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSVPCResourceController"
  role       = aws_iam_role.cluster.name
}

# =============================================================================
# EKS Node IAM Role (for Auto Mode nodes)
# =============================================================================

resource "aws_iam_role" "node" {
  name = "${local.cluster_name}-node-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = ["sts:AssumeRole"]
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "node_AmazonEKSWorkerNodeMinimalPolicy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodeMinimalPolicy"
  role       = aws_iam_role.node.name
}

resource "aws_iam_role_policy_attachment" "node_AmazonEC2ContainerRegistryPullOnly" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPullOnly"
  role       = aws_iam_role.node.name
}

# =============================================================================
# Permissions Boundary for Runner Pods (Issue #1204 — scoped, #596 fix)
# =============================================================================

resource "aws_iam_policy" "runner_boundary" {
  name        = "${var.project_name}-runner-boundary"
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

          # KMS
          "kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*",
          "kms:Describe*", "kms:Get*", "kms:List*",
          "kms:CreateKey", "kms:CreateGrant", "kms:RetireGrant",
          "kms:TagResource", "kms:UntagResource",
          "kms:ScheduleKeyDeletion", "kms:PutKeyPolicy",
          "kms:EnableKeyRotation", "kms:DisableKeyRotation",
          "kms:CreateAlias", "kms:DeleteAlias", "kms:UpdateAlias",

          # IAM reads
          "iam:Get*", "iam:List*", "iam:Simulate*", "iam:Generate*",

          # NOTE (A18, #5674): the IAM WRITE actions and sts:AssumeRole that used
          # to sit here have moved to the DenyPrivilegeEscalation statement
          # below. They were the finding: a boundary that permits
          # iam:CreateRole + iam:AttachRolePolicy does not bound anything, since
          # the role it caps can mint itself a fresh administrator role and use
          # it. The Terraform-driven IAM lifecycle these grants were added for
          # (#596) belongs to the deploy identity, not to a runner executing
          # third-party-authored workflow instructions.
          "sts:GetCallerIdentity",
          "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
          "events:*", "stepfunctions:*",
          "cognito-idp:*", "cognito-identity:*",
          "elasticache:*", "eks:*",
          "sagemaker:*",
          # WAFv2
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
        Sid      = "ExecuteApi"
        Effect   = "Allow"
        Action   = ["execute-api:*"]
        Resource = "*"
      },
      {
        # A18 (#5674). A Deny in a permissions boundary is absolute for the roles
        # it caps: no policy attached to a bounded role can grant these, so a
        # future edit to the onboarding script's inline policy — or to the
        # managed policies below — cannot reopen the path. That is the point of
        # denying here rather than only omitting from the grant.
        #
        # Every action listed lets the holder obtain an identity other than the
        # one it was issued. iam:PassRole is included because handing an existing
        # privileged role to a service the runner can invoke (Lambda, CodeBuild,
        # EC2) reaches administrator without creating anything. The three
        # sts:AssumeRole* variants are included because the shared runner role
        # reachable through them could read the whole adp/ secret prefix — every
        # tenant's stored credentials and the signing keys for agent control
        # messages and GitHub App auth.
        Sid    = "DenyPrivilegeEscalation"
        Effect = "Deny"
        Action = [
          "iam:AddClientIDToOpenIDConnectProvider",
          "iam:AddRoleToInstanceProfile",
          "iam:AddUserToGroup",
          "iam:AttachGroupPolicy",
          "iam:AttachRolePolicy",
          "iam:AttachUserPolicy",
          "iam:CreateGroup",
          "iam:CreateInstanceProfile",
          "iam:CreateOpenIDConnectProvider",
          "iam:CreatePolicy",
          "iam:CreatePolicyVersion",
          "iam:CreateRole",
          "iam:CreateSAMLProvider",
          "iam:CreateServiceLinkedRole",
          "iam:DeleteGroupPolicy",
          "iam:DeleteOpenIDConnectProvider",
          "iam:DeletePolicy",
          "iam:DeletePolicyVersion",
          "iam:DeleteRole",
          "iam:DeleteRolePermissionsBoundary",
          "iam:DeleteRolePolicy",
          "iam:DeleteUserPermissionsBoundary",
          "iam:DeleteUserPolicy",
          "iam:DetachGroupPolicy",
          "iam:DetachRolePolicy",
          "iam:DetachUserPolicy",
          "iam:PassRole",
          "iam:PutGroupPolicy",
          "iam:PutRolePermissionsBoundary",
          "iam:PutRolePolicy",
          "iam:PutUserPermissionsBoundary",
          "iam:PutUserPolicy",
          "iam:SetDefaultPolicyVersion",
          "iam:UpdateAssumeRolePolicy",
          "iam:UpdateOpenIDConnectProviderThumbprint",
          "iam:UpdateRole",
          "iam:UpdateUser",
          "sts:AssumeRole",
          "sts:AssumeRoleWithSAML",
          "sts:AssumeRoleWithWebIdentity"
        ]
        Resource = "*"
      },
      {
        # A18 (#5674). The onboarding script now grants each repository only its
        # own secret paths, and the shared role's grant is env-segmented — but
        # this boundary's ceiling is still secretsmanager:* on "*", so a future
        # policy edit could re-grant cross-tenant reads. A Deny here cannot be
        # out-voted by any attached policy, making that edit inert.
        #
        # Same four environment-less namespaces as the two sibling runner roles
        # (webhook-ingress/infra/scaledjob-iam.tf Sid DenyTenantVaultSecrets and
        # agent-factory/infra/modules/runner-iam/main.tf). Keep all three in
        # sync. PATH SHAPE IS LOAD-BEARING: vault paths carry no environment
        # segment, so "normalising" these to adp/${var.environment}/* would match
        # nothing while reading, in review, as though it still closed the hole.
        #
        # Both actions are required — a GetSecretValue-only Deny still lets a
        # runner enumerate other tenants' secret names.
        Sid    = "DenyTenantVaultSecrets"
        Effect = "Deny"
        Action = [
          "secretsmanager:DescribeSecret",
          "secretsmanager:GetSecretValue"
        ]
        Resource = [
          "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:adp/users/*",
          "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:adp/teams/*",
          "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:adp/orgs/*",
          "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:adp/domain-apps/*"
        ]
      },
      {
        Sid    = "DenyDangerousActions"
        Effect = "Deny"
        Action = [
          "iam:CreateUser",
          "iam:DeleteUser",
          "iam:CreateLoginProfile",
          "iam:UpdateLoginProfile",
          "iam:CreateAccessKey",
          "iam:UpdateAccessKey",
          "organizations:*",
          "account:*",
          "aws-portal:*",
          "billing:*",
          "budgets:ModifyBudget",
          "budgets:DeleteBudget",
          "ce:*"
        ]
        Resource = "*"
      }
    ]
  })
}

# =============================================================================
# IRSA Role for Runner Pods
# =============================================================================

resource "aws_iam_role" "runner" {
  name                 = "${var.project_name}-runner-role"
  permissions_boundary = aws_iam_policy.runner_boundary.arn

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_openid_connect_provider.eks.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      # A18 (#5674): exact match on the allowed service accounts, replacing
      # StringLike on "system:serviceaccount:arc-runners-*:github-runner-sa".
      #
      # The pattern was equivalent to "any namespace whose name starts
      # arc-runners-, with the conventional service-account name". Namespaces are
      # created per repository by scripts/onboard-repo.sh, and the service
      # account name is fixed by that script — so onboarding a repository
      # silently made its runner trusted by this role, which reaches this
      # module's shared grants. Nobody decided that; the wildcard did.
      #
      # With StringEquals on a list, a new runner namespace is NOT trusted until
      # its service account is added here and applied — which is the review step
      # the wildcard skipped. `aud` is asserted because a federated trust policy
      # that conditions only on `sub` accepts a token minted for a different
      # audience.
      Condition = {
        StringEquals = {
          "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub" = [
            for namespace in var.runner_trusted_namespaces :
            "system:serviceaccount:${namespace}:github-runner-sa"
          ]
          "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:aud" = "sts.amazonaws.com"
        }
      }
    }]
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
  name        = "${var.project_name}-runner-base"
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
  name        = "${var.project_name}-runner-services"
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
        # A18 (#5674): was "secret:adp/*", which spanned every tenant. The
        # platform mints per-customer vault secrets under the same adp/ prefix
        # with NO environment segment — adp/users/<sub>/…, adp/teams/<id>/…,
        # adp/orgs/<id>/… and adp/domain-apps/<app>/<org>/… (gateway/src/shared/
        # services/secrets_manager.py). So "adp/*" gave a runner read AND WRITE
        # over every customer's stored API keys and database passwords, and the
        # access was indistinguishable from ordinary deploy traffic.
        #
        # Now scoped to the environment-segmented platform paths deploys
        # actually touch. adp/${var.environment}/* cannot match a vault path,
        # because vault paths have no environment segment — that shape
        # difference is what makes this Allow sufficient without also needing a
        # Deny (contrast the scaledjob role, which must read a tenant-specific
        # path at runtime and therefore needs an explicit
        # DenyTenantVaultSecrets — see webhook-ingress/infra/scaledjob-iam.tf).
        #
        # Do NOT widen this back to adp/*: that single character reopens the
        # cross-tenant vault read. If a deploy needs another path, add that
        # path.
        Resource = [
          "arn:aws:secretsmanager:*:*:secret:bedrockgw-*",
          "arn:aws:secretsmanager:${var.aws_region}:*:secret:adp/${var.environment}/*",
          "arn:aws:secretsmanager:${var.aws_region}:*:secret:adp/runner/*",
          "arn:aws:secretsmanager:${var.aws_region}:*:secret:github-runner/*"
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
        # A18 (#5674): was "sts:AssumeRole" + "sts:GetCallerIdentity" on "*".
        #
        # AssumeRole on "*" is a lateral-movement primitive: it lets this role
        # become ANY role in the account whose trust policy accepts it, which is
        # how a scoped runner reaches grants it was deliberately not given.
        # Combined with the iam: writes this policy set used to hold, it was also
        # the second half of a self-promotion path — create a role with
        # AdministratorAccess, then assume it.
        #
        # The boundary now DENIES sts:AssumeRole (Sid DenyPrivilegeEscalation),
        # so this Allow had already become unreachable; leaving it would only
        # mislead the next reader into thinking the capability exists.
        #
        # GetCallerIdentity is kept: it is unprivileged (it reports who you
        # already are, grants nothing) and deploy scripts across this repo call
        # it to resolve the account ID.
        Sid      = "CallerIdentity"
        Effect   = "Allow"
        Action   = ["sts:GetCallerIdentity"]
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
