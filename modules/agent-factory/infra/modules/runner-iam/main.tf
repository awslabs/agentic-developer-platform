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
          # rds-db:connect is a SEPARATE service namespace from rds:* — it is
          # NOT covered by "rds:*". Required for IAM database authentication
          # (aws rds generate-db-auth-token) now that the gateway RDS master
          # user is IAM-auth-only (BG_RDS_IAM_AUTH=true); password auth is
          # disabled, so DB-touching workflows (evals, schema-check, seeding)
          # must connect with an IAM token.
          "rds-db:connect",
          "ecs:*", "ecr:*", "elasticloadbalancing:*", "autoscaling:*",
          "cloudformation:*", "cloudwatch:*", "logs:*", "sns:*", "sqs:*",
          "apigateway:*", "route53:*", "cloudfront:*", "acm:*",
          "amplify:*", "codebuild:*",
          "secretsmanager:*",
          "ssm:*",

          # securityagent — the nightly whole-repo code review (#4443/#4445)
          # runs on arc-runner-org under this role. A boundary caps the role's
          # effective permissions regardless of its attached policies, so
          # omitting this namespace denied UpdateAgentSpace with "because no
          # permissions boundary allows the securityagent:UpdateAgentSpace
          # action" even though AdministratorAccess is attached (#4470).
          #
          # This grants only the ability to CALL the service. It does not
          # widen the blast radius of what a review can do: the two dangerous
          # settings (live-validation mode, auto-remediation) are pinned off in
          # code_review_request.py with no caller parameter, and the service
          # itself acts through adp-<env>-securityagent-nightly, whose own
          # least-privilege policy is unchanged by this.
          "securityagent:*",

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
          # EPIC #4997: platform Terraform owns regional invocation logging.
          "bedrock:GetModelInvocationLoggingConfiguration",
          "bedrock:PutModelInvocationLoggingConfiguration",
          "bedrock:DeleteModelInvocationLoggingConfiguration",
          # Read/agreement APIs for platform/scripts/enable-bedrock-models.sh,
          # a prerequisite step of platform-infra-apply.yml. The boundary caps
          # the runner's effective permissions, so omitting these here denies
          # them regardless of the services policy.
          "bedrock:ListFoundationModels", "bedrock:GetFoundationModel",
          "bedrock:GetFoundationModelAvailability",
          "bedrock:ListFoundationModelAgreementOffers",
          "bedrock:CreateFoundationModelAgreement",
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
          "cloudtrail:*",
          # S3 Vectors — agent-context code embeddings (Issue #1406)
          "s3vectors:*",
          # Neptune — agent-context verify gate queries (Issue #1553)
          "neptune-db:*"
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
        # Cross-tenant vault lockout — DEFENCE IN DEPTH ONLY (issue #4130).
        #
        # This does NOT close #4073 finding #4 for this role. The binding grant
        # here is aws_iam_policy.runner_base Sid AllowBroadAccess, which holds
        # secretsmanager:* on Resource="*" — broader than this finding and
        # tracked separately in #4116. Do not mark #4116 addressed by this.
        #
        # Placed in the BOUNDARY rather than in runner_base on purpose: the
        # boundary is the ceiling on this role's effective permissions, so a
        # Deny here is reachable regardless of what any attached policy allows
        # and cannot be out-voted by the secretsmanager:* grant above. A Deny
        # added to runner_base instead would be trivially bypassed the moment a
        # future policy re-granted the same actions elsewhere.
        #
        # Same four env-less namespaces as the scaledjob role — see
        # webhook-ingress/infra/scaledjob-iam.tf Sid DenyTenantVaultSecrets for
        # the full rationale and the path-shape warning. Keep the two in sync.
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
        # SARIF archival (date-partitioned) + the per-run "findings/<run_id>/"
        # rendezvous prefix the Security Summary job reads instead of GitHub
        # Actions artifacts (which were unreliable under storage-quota pressure).
        Sid    = "SecurityScanUpload"
        Effect = "Allow"
        Action = ["s3:PutObject"]
        Resource = [
          "${var.security_scans_bucket_arn}/sarif/*",
          "${var.security_scans_bucket_arn}/findings/*"
        ]
      },
      {
        # Summary / nightly baseline jobs sync the rendezvous prefix back down.
        Sid      = "SecurityScanReadFindings"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${var.security_scans_bucket_arn}/findings/*"
      },
      {
        # s3 sync lists the prefix before downloading.
        Sid      = "SecurityScanListFindings"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = var.security_scans_bucket_arn
        Condition = {
          StringLike = {
            "s3:prefix" = ["findings/*"]
          }
        }
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
        # Issue #4450 / design note #4559 §5. The nightly security pipeline's ONE
        # root dispatch per night is an `aws events put-events` from this runner —
        # a GitHub Actions job cannot originate an agent chain any other way
        # (adp-trigger and /agent/trigger are both closed to root-minting).
        #
        # This CANNOT join EventBridgeRules above: that statement's resource is a
        # RULE arn, while PutEvents authorizes against the EVENT-BUS arn.
        #
        # State the scope honestly: `events:PutEvents` cannot be restricted to an
        # event `source`. The IAM resource is the bus; `source` is a request-body
        # field with no condition key. So this grant lets the runner emit any
        # event with any source onto the default bus, including one matching
        # another enabled adp-<env>-* rule. What contains it is the target rules'
        # InputTransformers, which pin persona, service_identity and target repo
        # as Terraform literals, plus `allowed_personas` on each service-identity
        # row. Blast radius therefore equals the set of ENABLED adp-<env>-* rules
        # — one, today — and each future rule widens it. This is "scoped to the
        # bus, with source-level selection constrained by the transformer," not
        # least privilege.
        Sid    = "EventBridgePutEvents"
        Effect = "Allow"
        Action = [
          "events:PutEvents"
        ]
        Resource = "arn:aws:events:us-east-1:*:event-bus/default"
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
      # Model-access enablement, used by platform/scripts/enable-bedrock-models.sh
      # which runs as a prerequisite step in platform-infra-apply.yml. Without
      # these the script fails before Terraform is ever reached, so the whole
      # platform apply is unrunnable. These are account-level read/agreement
      # APIs that take no resource qualifier, hence Resource = "*".
      {
        Sid    = "BedrockModelAccessEnablement"
        Effect = "Allow"
        Action = [
          "bedrock:ListFoundationModels",
          "bedrock:GetFoundationModel",
          "bedrock:GetFoundationModelAvailability",
          "bedrock:ListFoundationModelAgreementOffers",
          "bedrock:CreateFoundationModelAgreement"
        ]
        Resource = ["*"]
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
          # Read-only. Needed by the nightly's findings-bucket privacy
          # assertion (#4533), which runs as this role because the code-review
          # job has no configure-aws-credentials step. Without it the step fails
          # AccessDenied for a permissions reason rather than reporting on the
          # bucket.
          "s3:GetBucketPolicyStatus",
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
        Sid    = "S3VectorsMgmt"
        Effect = "Allow"
        Action = [
          "s3vectors:CreateVectorBucket",
          "s3vectors:DeleteVectorBucket",
          "s3vectors:GetVectorBucket",
          "s3vectors:ListVectorBuckets",
          "s3vectors:CreateIndex",
          "s3vectors:DeleteIndex",
          "s3vectors:GetIndex",
          "s3vectors:ListIndexes"
        ]
        Resource = "arn:aws:s3vectors:us-east-1:*:vector-bucket/adp-*"
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
        Sid    = "NeptuneDataAccess"
        Effect = "Allow"
        Action = [
          "neptune-db:ReadDataViaQuery",
          "neptune-db:WriteDataViaQuery",
          "neptune-db:DeleteDataViaQuery",
          "neptune-db:GetQueryStatus",
          "neptune-db:CancelQuery"
        ]
        Resource = "arn:aws:neptune-db:us-east-1:*:cluster-*/*"
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
