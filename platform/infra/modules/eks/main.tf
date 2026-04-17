# Data sources
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# KMS Key for EKS secrets encryption
resource "aws_kms_key" "eks_secrets" {
  description             = "${var.name_prefix}-eks-secrets"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-kms-eks-secrets"
    Service = "kms"
    Purpose = "eks-secrets-encryption"
  })
}

# KMS Key Alias for easier identification
resource "aws_kms_alias" "eks_secrets" {
  name          = "alias/${var.name_prefix}-eks-secrets"
  target_key_id = aws_kms_key.eks_secrets.key_id
}

# EKS Cluster with Auto Mode
resource "aws_eks_cluster" "main" {
  name     = "${var.name_prefix}-eks-cluster"
  role_arn = var.eks_cluster_role_arn
  version  = var.cluster_version

  # Required for EKS Auto Mode
  bootstrap_self_managed_addons = false

  access_config {
    authentication_mode = "API_AND_CONFIG_MAP"
  }

  vpc_config {
    subnet_ids              = var.private_subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = var.eks_public_access_cidrs
    security_group_ids      = [var.eks_security_group_id]
  }

  # Enable EKS secrets encryption with KMS
  encryption_config {
    provider {
      key_arn = aws_kms_key.eks_secrets.arn
    }
    resources = ["secrets"]
  }

  # EKS Auto Mode — AWS manages compute, networking, and storage
  compute_config {
    enabled       = true
    node_pools    = ["general-purpose"]
    node_role_arn = var.node_group_role_arn
  }

  kubernetes_network_config {
    elastic_load_balancing {
      enabled = true
    }
  }

  storage_config {
    block_storage {
      enabled = true
    }
  }

  # Enable logging
  enabled_cluster_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]

  depends_on = [aws_kms_key.eks_secrets]

  tags = merge(var.common_tags, {
    Name                                                   = "${var.name_prefix}-eks-cluster"
    "kubernetes.io/cluster/${var.name_prefix}-eks-cluster" = "owned"
  })
}

# OIDC Provider for IRSA (IAM Roles for Service Accounts)
data "tls_certificate" "cluster" {
  url = aws_eks_cluster.main.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "cluster" {
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.cluster.certificates[0].sha1_fingerprint]
  url             = aws_eks_cluster.main.identity[0].oidc[0].issuer

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-eks-oidc"
  })
}

# Access entries + cluster-admin policy for operators/CI.
# Without these, Kubernetes provider calls below fail with Unauthorized on
# first apply because the cluster creator isn't auto-granted system:masters
# in API auth mode.
resource "aws_eks_access_entry" "admins" {
  for_each      = toset(var.cluster_admin_principal_arns)
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = each.key
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "admins" {
  for_each      = aws_eks_access_entry.admins
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = each.value.principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }
}

# Kubernetes Namespace for Gateway Service
# Note: This namespace is also defined in k8s/namespace.yaml for the deploy workflow.
# Terraform creates it here to ensure the service account can be created.
# kubectl apply -f k8s/namespace.yaml is idempotent and won't conflict.
resource "kubernetes_namespace" "bedrockgw" {
  metadata {
    name = "bedrockgw"
    labels = {
      "app.kubernetes.io/name"    = "bedrockgateway"
      "app.kubernetes.io/part-of" = "bedrock-gateway"
    }
  }

  depends_on = [
    aws_eks_cluster.main,
    aws_eks_access_policy_association.admins,
  ]
}

# IRSA Role for Gateway Service — trusts the EKS OIDC provider
# This role is created here (not in the IAM module) because it depends on
# the OIDC provider which is created after the EKS cluster.
locals {
  oidc_issuer = replace(aws_eks_cluster.main.identity[0].oidc[0].issuer, "https://", "")
}

resource "aws_iam_role" "gateway_service_irsa" {
  name = "${var.name_prefix}-role-gateway-service"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.cluster.arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
            "${local.oidc_issuer}:sub" = "system:serviceaccount:bedrockgw:gateway-service"
          }
        }
      }
    ]
  })

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-role-gateway-service"
    Service = "iam"
    Purpose = "gateway-service-irsa"
  })
}

# Bedrock invoke permissions for the gateway service
# Issue #133: SECURITY FIX - Scoped Resource ARNs instead of "*"
# Previously used Resource = "*" which is overly permissive
# Now scoped to specific model patterns and this account's resources
resource "aws_iam_role_policy" "gateway_bedrock_invoke" {
  name = "${var.name_prefix}-policy-gateway-bedrock"
  role = aws_iam_role.gateway_service_irsa.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "BedrockInvokeModels"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream"
        ]
        # Issue #133: Scoped to specific model families and inference profiles
        # Using wildcards for model versions but restricted to allowed providers
        Resource = [
          # Anthropic Claude models
          "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
          # Amazon Titan models
          "arn:aws:bedrock:*::foundation-model/amazon.titan-*",
          # Amazon Nova models
          "arn:aws:bedrock:*::foundation-model/amazon.nova-*",
          # AI21 models (if needed)
          "arn:aws:bedrock:*::foundation-model/ai21.*",
          # Cohere models (if needed)
          "arn:aws:bedrock:*::foundation-model/cohere.*",
          # Meta Llama models
          "arn:aws:bedrock:*::foundation-model/meta.llama*",
          # Mistral models
          "arn:aws:bedrock:*::foundation-model/mistral.*",
          # Cross-region inference profiles (scoped to this account)
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*"
        ]
      },
      {
        Sid    = "BedrockListModels"
        Effect = "Allow"
        Action = [
          "bedrock:ListFoundationModels",
          "bedrock:ListInferenceProfiles",
          "bedrock:GetInferenceProfile"
        ]
        # List operations need * but are read-only
        Resource = "*"
      }
    ]
  })
}

# STS permissions for cross-account pool access
resource "aws_iam_role_policy" "gateway_sts" {
  name = "${var.name_prefix}-policy-gateway-sts"
  role = aws_iam_role.gateway_service_irsa.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sts:GetCallerIdentity"]
        Resource = "*"
      }
    ]
  })
}

# CloudWatch Logs permissions for the gateway service
resource "aws_iam_role_policy" "gateway_logs" {
  name = "${var.name_prefix}-policy-gateway-irsa-logs"
  role = aws_iam_role.gateway_service_irsa.id

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
        Resource = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/eks/*"
      }
    ]
  })
}

# RDS IAM Authentication for the gateway IRSA role
resource "aws_iam_role_policy" "gateway_rds_iam_auth" {
  count = var.enable_rds_iam_auth ? 1 : 0
  name  = "${var.name_prefix}-policy-gateway-rds-iam-auth"
  role  = aws_iam_role.gateway_service_irsa.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["rds-db:connect"]
        Resource = "arn:aws:rds-db:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:dbuser:*/${var.rds_db_username}"
      }
    ]
  })
}

# ElastiCache IAM Authentication for the gateway IRSA role
resource "aws_iam_role_policy" "gateway_elasticache_iam_auth" {
  count = var.enable_elasticache_iam_auth && var.redis_replication_group_id != "" && var.redis_iam_user_id != "" ? 1 : 0
  name  = "${var.name_prefix}-policy-gateway-elasticache-iam-auth"
  role  = aws_iam_role.gateway_service_irsa.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["elasticache:Connect"]
        Resource = [
          "arn:aws:elasticache:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:replicationgroup:${var.redis_replication_group_id}",
          "arn:aws:elasticache:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:user:${var.redis_iam_user_id}"
        ]
      }
    ]
  })
}

# Cross-account Bedrock pool assume role (if pool accounts configured)
resource "aws_iam_role_policy" "gateway_cross_account" {
  count = length(var.pool_account_arns) > 0 ? 1 : 0
  name  = "${var.name_prefix}-policy-gateway-cross-account"
  role  = aws_iam_role.gateway_service_irsa.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sts:AssumeRole"]
        Resource = [for acct in var.pool_account_arns : "arn:aws:iam::${acct}:role/*BedrockGateway-Pool*"]
      }
    ]
  })
}

# DynamoDB access for agent client lookups (pre-token-generation context)
resource "aws_iam_role_policy" "gateway_dynamodb" {
  name = "${var.name_prefix}-policy-gateway-dynamodb"
  role = aws_iam_role.gateway_service_irsa.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:Scan", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = [
          "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/bedrockgw-*",
          "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/bedrockgw-*/*"
        ]
      }
    ]
  })
}

# Cognito read permissions for Cognito-backed entity list endpoints
# Issue #226: Cognito as single source of truth for users/teams/orgs
resource "aws_iam_role_policy" "gateway_cognito_read" {
  count = var.cognito_user_pool_id != "" ? 1 : 0
  name  = "${var.name_prefix}-policy-gateway-cognito-read"
  role  = aws_iam_role.gateway_service_irsa.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CognitoReadUsers"
        Effect = "Allow"
        Action = [
          "cognito-idp:ListUsers",
          "cognito-idp:ListGroups",
          "cognito-idp:ListUsersInGroup",
          "cognito-idp:AdminGetUser"
        ]
        # Scoped to specific user pool if provided, otherwise any pool in this account
        Resource = var.cognito_user_pool_id != "" ? "arn:aws:cognito-idp:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:userpool/${var.cognito_user_pool_id}" : "arn:aws:cognito-idp:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:userpool/*"
      }
    ]
  })
}

# S3 chat logs write permissions for the gateway IRSA role
# Issue #143: Chat logging writes conversation logs to S3
resource "aws_iam_role_policy" "gateway_chat_logs_s3" {
  count = var.chat_logs_bucket_arn != "" ? 1 : 0
  name  = "${var.name_prefix}-policy-gateway-chat-logs-s3"
  role  = aws_iam_role.gateway_service_irsa.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ChatLogsS3Write"
        Effect = "Allow"
        Action = [
          "s3:PutObject"
        ]
        Resource = "${var.chat_logs_bucket_arn}/*"
      },
      {
        Sid    = "ChatLogsS3BucketAccess"
        Effect = "Allow"
        Action = [
          "s3:GetBucketLocation"
        ]
        Resource = var.chat_logs_bucket_arn
      }
    ]
  })
}

# Comprehend PII detection permissions for the gateway IRSA role
# Issue #143: Standard scrub level uses Comprehend to detect PII entities
resource "aws_iam_role_policy" "gateway_comprehend_pii" {
  count = var.enable_comprehend_pii ? 1 : 0
  name  = "${var.name_prefix}-policy-gateway-comprehend-pii"
  role  = aws_iam_role.gateway_service_irsa.id

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

# Kubernetes Service Account for Gateway Service — annotated with IRSA role
resource "kubernetes_service_account" "gateway_service" {
  metadata {
    name      = "gateway-service"
    namespace = "bedrockgw"
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.gateway_service_irsa.arn
    }
  }

  depends_on = [
    aws_eks_cluster.main,
    kubernetes_namespace.bedrockgw
  ]
}

# Security group rules for cluster communication
resource "aws_security_group_rule" "cluster_ingress_node_https" {
  description              = "Allow pods to communicate with the cluster API Server"
  from_port                = 443
  protocol                 = "tcp"
  security_group_id        = aws_eks_cluster.main.vpc_config[0].cluster_security_group_id
  source_security_group_id = var.eks_security_group_id
  to_port                  = 443
  type                     = "ingress"
}

# =============================================================================
# Container Insights (CloudWatch Observability Addon)
# =============================================================================
# Deploys the CloudWatch agent + Fluent Bit to ship pod application logs
# and Container Insights metrics to CloudWatch. This gives us:
# - Pod logs in CloudWatch Logs Insights (including gateway timing breakdowns)
# - Container-level CPU/memory/network metrics
# - Application-level structured log queries

# IAM role for the CloudWatch Observability addon (IRSA)
resource "aws_iam_role" "cloudwatch_observability" {
  count = var.enable_container_insights ? 1 : 0
  name  = "${var.name_prefix}-role-cw-observability"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.cluster.arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
            "${local.oidc_issuer}:sub" = "system:serviceaccount:amazon-cloudwatch:cloudwatch-agent"
          }
        }
      }
    ]
  })

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-role-cw-observability"
    Service = "iam"
    Purpose = "cloudwatch-observability-addon"
  })
}

resource "aws_iam_role_policy_attachment" "cloudwatch_observability" {
  count      = var.enable_container_insights ? 1 : 0
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
  role       = aws_iam_role.cloudwatch_observability[0].name
}

resource "aws_eks_addon" "cloudwatch_observability" {
  count        = var.enable_container_insights ? 1 : 0
  cluster_name = aws_eks_cluster.main.name
  addon_name   = "amazon-cloudwatch-observability"

  service_account_role_arn = aws_iam_role.cloudwatch_observability[0].arn

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [
    aws_eks_cluster.main,
    aws_iam_role.cloudwatch_observability,
  ]

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-addon-cw-observability"
    Service = "cloudwatch"
    Purpose = "container-insights"
  })
}

# CI runner EKS access is managed in the workflow pre-apply step
# to avoid chicken-and-egg: runner needs access to run Terraform,
# but Terraform would create the access entry
