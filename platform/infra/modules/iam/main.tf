# Data sources
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# EKS Cluster Service Role
resource "aws_iam_role" "eks_cluster" {
  name = "${var.name_prefix}-role-eks-cluster"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = ["sts:AssumeRole", "sts:TagSession"]
        Effect = "Allow"
        Principal = {
          Service = "eks.amazonaws.com"
        }
      }
    ]
  })

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-role-eks-cluster"
    Service = "iam"
    Purpose = "eks-cluster"
  })
}

# EKS Cluster Service Role Policy Attachments
# Auto Mode requires Compute/BlockStorage/LoadBalancing/Networking policies
# on the cluster role so Karpenter (AWS-managed) can create instance profiles,
# ELBs, and EBS volumes on demand.
resource "aws_iam_role_policy_attachment" "eks_cluster_policy" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
    "arn:aws:iam::aws:policy/AmazonEKSComputePolicy",
    "arn:aws:iam::aws:policy/AmazonEKSBlockStoragePolicy",
    "arn:aws:iam::aws:policy/AmazonEKSLoadBalancingPolicy",
    "arn:aws:iam::aws:policy/AmazonEKSNetworkingPolicy",
  ])
  policy_arn = each.key
  role       = aws_iam_role.eks_cluster.name
}

# EKS Node Group Service Role
resource "aws_iam_role" "eks_node_group" {
  name = "${var.name_prefix}-role-eks-node-group"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-role-eks-node-group"
    Service = "iam"
    Purpose = "eks-node-group"
  })
}

# EKS Node Group Service Role Policy Attachments (Auto Mode)
# Auto Mode uses AmazonEKSWorkerNodeMinimalPolicy + AmazonEC2ContainerRegistryPullOnly.
# We also keep the legacy Worker/CNI/ECR-ReadOnly policies for compatibility
# with classic managed node groups in case the cluster ever runs in mixed mode.
resource "aws_iam_role_policy_attachment" "eks_node_group_policies" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodeMinimalPolicy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPullOnly",
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
  ])
  policy_arn = each.key
  role       = aws_iam_role.eks_node_group.name
}

# Gateway Service Role (will be created after OIDC provider exists)
# This is a placeholder - actual IRSA role will be created after EKS cluster
resource "aws_iam_role" "gateway_service_placeholder" {
  name = "${var.name_prefix}-role-gateway-service-placeholder"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-role-gateway-service-placeholder"
    Service = "iam"
    Purpose = "gateway-service-placeholder"
  })
}

# Gateway Service Policy - STS and Cross-Account Assume Role
resource "aws_iam_role_policy" "gateway_service_policy" {
  name = "${var.name_prefix}-policy-gateway-service"
  role = aws_iam_role.gateway_service_placeholder.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Effect = "Allow"
          Action = [
            "sts:GetCallerIdentity"
          ]
          Resource = "*"
        }
      ],
      # Only include cross-account assume role statement when pool_account_arns is not empty
      length(var.pool_account_arns) > 0 ? [
        {
          Effect = "Allow"
          Action = [
            "sts:AssumeRole"
          ]
          Resource = [
            for account_arn in var.pool_account_arns :
            "arn:aws:iam::${account_arn}:role/*BedrockGateway-Pool*"
          ]
        }
      ] : []
    )
  })
}

# Instance Profile for EKS Node Group
resource "aws_iam_instance_profile" "eks_node_group" {
  count = var.create_instance_profile ? 1 : 0
  name  = "${var.name_prefix}-instance-profile-eks-nodes"
  role  = aws_iam_role.eks_node_group.name

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-instance-profile-eks-nodes"
    Service = "iam"
    Purpose = "eks-node-group"
  })
}

# RDS IAM Database Authentication Policy for Gateway Service
# Allows the gateway pod to authenticate to RDS using IAM auth tokens
resource "aws_iam_role_policy" "gateway_service_rds_iam_auth" {
  count = var.enable_rds_iam_auth ? 1 : 0
  name  = "${var.name_prefix}-policy-gateway-rds-iam-auth"
  role  = aws_iam_role.gateway_service_placeholder.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "rds-db:connect"
        ]
        Resource = "arn:aws:rds-db:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:dbuser:*/${var.rds_db_username}"
      }
    ]
  })
}

# ElastiCache IAM Authentication Policy for Gateway Service
# Allows the gateway pod to authenticate to ElastiCache Redis using IAM auth tokens
# This enables passwordless authentication similar to RDS IAM auth
resource "aws_iam_role_policy" "gateway_service_elasticache_iam_auth" {
  count = var.enable_elasticache_iam_auth && var.redis_replication_group_id != "" && var.redis_iam_user_id != "" ? 1 : 0
  name  = "${var.name_prefix}-policy-gateway-elasticache-iam-auth"
  role  = aws_iam_role.gateway_service_placeholder.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["elasticache:Connect"]
        Resource = [
          "arn:aws:elasticache:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:replicationgroup:${var.redis_replication_group_id}",
          "arn:aws:elasticache:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:user:${var.redis_iam_user_id}"
        ]
      }
    ]
  })
}

# Additional policies for gateway service (if needed)
resource "aws_iam_role_policy" "gateway_service_logs" {
  name = "${var.name_prefix}-policy-gateway-logs"
  role = aws_iam_role.gateway_service_placeholder.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams"
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:log-group:/aws/eks/*"
      }
    ]
  })
}

# Policy for ECR access from EKS nodes
resource "aws_iam_role_policy" "eks_node_group_ecr" {
  name = "${var.name_prefix}-policy-eks-nodes-ecr"
  role = aws_iam_role.eks_node_group.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
          "ecr:GetAuthorizationToken"
        ]
        Resource = "*"
      }
    ]
  })
}

# =============================================================================
# Chat Logging Permissions (Issue #143)
# =============================================================================
# S3 permissions for async chat log writes
resource "aws_iam_role_policy" "gateway_service_chat_logs_s3" {
  count = var.enable_chat_logging ? 1 : 0
  name  = "${var.name_prefix}-policy-gateway-chat-logs-s3"
  role  = aws_iam_role.gateway_service_placeholder.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ChatLogS3Write"
        Effect = "Allow"
        Action = [
          "s3:PutObject"
        ]
        Resource = "${var.chat_logs_bucket_arn}/*"
      },
      {
        Sid    = "ChatLogS3BucketAccess"
        Effect = "Allow"
        Action = [
          "s3:GetBucketLocation"
        ]
        Resource = var.chat_logs_bucket_arn
      }
    ]
  })
}

# Comprehend permissions for PII detection (standard scrubbing level)
resource "aws_iam_role_policy" "gateway_service_comprehend_pii" {
  count = var.enable_chat_logging && var.enable_comprehend_pii ? 1 : 0
  name  = "${var.name_prefix}-policy-gateway-comprehend-pii"
  role  = aws_iam_role.gateway_service_placeholder.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ComprehendPiiDetection"
        Effect = "Allow"
        Action = [
          "comprehend:DetectPiiEntities"
        ]
        Resource = "*"
      }
    ]
  })
}

# Issue #144: X-Ray Tracing Permissions for Gateway Service
# Allows the gateway pod to send trace data to AWS X-Ray
resource "aws_iam_role_policy" "gateway_service_xray" {
  count = var.enable_xray_tracing ? 1 : 0
  name  = "${var.name_prefix}-policy-gateway-xray"
  role  = aws_iam_role.gateway_service_placeholder.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
          "xray:GetSamplingRules",
          "xray:GetSamplingTargets"
        ]
        Resource = "*"
      }
    ]
  })
}

# =============================================================================
# Issue #134: Vault Phase 1 — Secrets Manager access for user credentials
# =============================================================================
# Gateway service role gains CRUD access to secrets under adp/users/*/*.
# Agent runner pods do NOT receive these permissions.
resource "aws_iam_role_policy" "gateway_service_vault_secrets" {
  count = var.enable_vault_secrets ? 1 : 0
  name  = "${var.name_prefix}-policy-gateway-vault-secrets"
  role  = aws_iam_role.gateway_service_placeholder.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "VaultSecretsManagerCRUD"
        Effect = "Allow"
        Action = [
          "secretsmanager:CreateSecret",
          "secretsmanager:GetSecretValue",
          "secretsmanager:UpdateSecret",
          "secretsmanager:DeleteSecret",
          "secretsmanager:DescribeSecret",
          "secretsmanager:ListSecrets"
        ]
        Resource = "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:adp/users/*/*"
      }
    ]
  })
}

# Cross-Account Bedrock Pool Role Templates (for documentation)
locals {
  bedrock_pool_trust_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          AWS = aws_iam_role.gateway_service_placeholder.arn
        }
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "sts:ExternalId" = var.name_prefix
          }
        }
      }
    ]
  })

  bedrock_pool_permissions_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:ListInferenceProfiles"
        ]
        Resource = "*"
      }
    ]
  })
}