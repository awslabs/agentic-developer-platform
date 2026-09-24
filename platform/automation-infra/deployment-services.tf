resource "aws_iam_policy" "deployment_base" {
  name        = "${var.name_prefix}-deployment-base"
  description = "Trusted deployment policy — infrastructure (EC2, EKS, IAM, KMS, CloudTrail, EventBridge, CodeBuild, ECR, ELB)"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "APIGatewayMgmt"
        Effect = "Allow"
        Action = [
          "apigateway:*"
        ]
        Resource = "arn:aws:apigateway:${var.aws_region}::/*"
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
        Resource = "arn:aws:cloudtrail:${var.aws_region}:${data.aws_caller_identity.current.account_id}:trail/adp-*-trail"
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
        Resource = "arn:aws:codebuild:${var.aws_region}:${data.aws_caller_identity.current.account_id}:project/adp-*"
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
          "arn:aws:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/adp-*"
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
          "arn:aws:eks:${var.aws_region}:${data.aws_caller_identity.current.account_id}:access-entry/adp-*-eks-cluster/*",
          "arn:aws:eks:${var.aws_region}:${data.aws_caller_identity.current.account_id}:addon/adp-*-eks-cluster/*/*"
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
        Resource = "arn:aws:events:${var.aws_region}:${data.aws_caller_identity.current.account_id}:rule/*-ecr-image-push"
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
        Resource = "arn:aws:events:${var.aws_region}:${data.aws_caller_identity.current.account_id}:event-bus/default"
      },
      {
        Sid    = "IAMReadOnly"
        Effect = "Allow"
        Action = [
          "iam:GetInstanceProfile",
          "iam:GetOpenIDConnectProvider",
          "iam:GetPolicy",
          "iam:GetPolicyVersion",
          "iam:GetRole",
          "iam:GetRolePolicy",
          "iam:ListAttachedRolePolicies",
          "iam:ListInstanceProfilesForRole",
          "iam:ListPolicies",
          "iam:ListPolicyVersions",
          "iam:ListRolePolicies",
          "iam:ListRoleTags",
          "iam:ListRoles"
        ]
        Resource = "*"
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

resource "aws_iam_policy" "deployment_services" {
  name        = "${var.name_prefix}-deployment-services"
  description = "Trusted deployment policy — application deploy (S3, Secrets, SSM, CloudFront, Lambda, SQS, DynamoDB, Logs, WAF, STS, Bedrock, CloudWatch)"

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
          "bedrock:GetInferenceProfile",
          "bedrock:GetUseCaseForModelAccess",
          "bedrock:PutUseCaseForModelAccess",
          "bedrock:ListFoundationModelAgreementOffers",
          "bedrock:CreateFoundationModelAgreement",
          "aws-marketplace:Subscribe",
          "aws-marketplace:ViewSubscriptions"
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
          "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/adp/*",
          "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/ecr/adp-*",
          "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/eks/adp-*:*",
          "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/github-ccsdk-agent/*"
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
        Resource = "arn:aws:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/adp-*"
      },
      {
        Sid    = "ExecuteAPIInvoke"
        Effect = "Allow"
        Action = [
          "execute-api:Invoke"
        ]
        Resource = [
          "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/*/agent/*",
          "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/*/internal/*"
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
        Resource = "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:adp-*"
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
          "secretsmanager:PutSecretValue",
          "secretsmanager:RestoreSecret",
          "secretsmanager:TagResource",
          "secretsmanager:UntagResource",
          "secretsmanager:UpdateSecret"
        ]
        Resource = [
          "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:bedrockgw-*",
          "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:adp/${var.environment}/*"
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
        Resource = "arn:aws:sqs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:adp-*"
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
        Resource = "arn:aws:s3vectors:${var.aws_region}:${data.aws_caller_identity.current.account_id}:vector-bucket/adp-*"
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
        Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/*"
      },
      {
        Sid      = "STSIdentity"
        Effect   = "Allow"
        Action   = ["sts:GetCallerIdentity"]
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
        Resource = "arn:aws:neptune-db:${var.aws_region}:${data.aws_caller_identity.current.account_id}:cluster-*/*"
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

resource "aws_iam_role_policy" "deployment_secret_reads" {
  count  = length(var.deployment_secret_arns) == 0 ? 0 : 1
  role   = aws_iam_role.deployment.id
  name   = "exact-deployment-secret-inputs"
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = var.deployment_secret_arns }] })
}

resource "aws_iam_role_policy_attachment" "deployment_base" {
  role       = aws_iam_role.deployment.name
  policy_arn = aws_iam_policy.deployment_base.arn
}

resource "aws_iam_role_policy_attachment" "deployment_services" {
  role       = aws_iam_role.deployment.name
  policy_arn = aws_iam_policy.deployment_services.arn
}
