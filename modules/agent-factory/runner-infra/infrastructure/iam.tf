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
# Permissions Boundary for Runner Pods
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
          "amplify:*",
          "secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret",
          "ssm:*", "kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*",
          "iam:CreateRole", "iam:CreatePolicy", "iam:AttachRolePolicy",
          "iam:PutRolePolicy", "iam:PassRole", "iam:TagRole", "iam:TagPolicy",
          "iam:CreateServiceLinkedRole", "iam:GetRole", "iam:GetPolicy",
          "iam:GetRolePolicy", "iam:ListRolePolicies",
          "iam:ListRoles", "iam:ListPolicies", "iam:ListAttachedRolePolicies",
          "iam:DeleteRole", "iam:DeleteRolePolicy", "iam:DetachRolePolicy",
          "sts:AssumeRole", "sts:GetCallerIdentity",
          "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
          "events:*", "stepfunctions:*", "cognito-idp:*", "elasticache:*",
          "eks:DescribeCluster", "eks:ListClusters",
          "sagemaker:*"
        ]
        Resource = "*"
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
      Condition = {
        StringLike = {
          "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub" = "system:serviceaccount:arc-runners-*:github-runner-sa"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "runner_permissions" {
  name = "runner-permissions"
  role = aws_iam_role.runner.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "BroadAWSAccess"
        Effect = "Allow"
        Action = [
          "ec2:*", "s3:*", "lambda:*", "dynamodb:*", "rds:*",
          "ecs:*", "ecr:*", "elasticloadbalancing:*", "autoscaling:*",
          "cloudformation:*", "cloudwatch:*", "logs:*", "sns:*", "sqs:*",
          "apigateway:*", "route53:*", "cloudfront:*", "acm:*",
          "amplify:*",
          "ssm:*", "events:*", "stepfunctions:*", "cognito-idp:*",
          "elasticache:*", "eks:DescribeCluster", "eks:ListClusters"
        ]
        Resource = "*"
      },
      {
        Sid    = "BedrockAccess"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream"
        ]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/anthropic.*",
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*"
        ]
      },
      {
        Sid    = "SecretsManagerAccess"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret"
        ]
        Resource = "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:github-*"
      },
      {
        Sid    = "KMSAccess"
        Effect = "Allow"
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:GenerateDataKey*"
        ]
        Resource = "*"
      },
      {
        Sid    = "IAMPassRole"
        Effect = "Allow"
        Action = [
          "iam:CreateRole",
          "iam:CreatePolicy",
          "iam:AttachRolePolicy",
          "iam:PutRolePolicy",
          "iam:PassRole",
          "iam:TagRole",
          "iam:TagPolicy",
          "iam:CreateServiceLinkedRole",
          "iam:GetRole",
          "iam:GetPolicy",
          "iam:GetRolePolicy",
          "iam:ListRoles",
          "iam:ListPolicies",
          "iam:ListRolePolicies",
          "iam:ListAttachedRolePolicies",
          "iam:DeleteRole",
          "iam:DeleteRolePolicy",
          "iam:DetachRolePolicy"
        ]
        Resource = "*"
      },
      {
        Sid    = "SageMakerAccess"
        Effect = "Allow"
        Action = [
          "sagemaker:*"
        ]
        Resource = "*"
      },
      {
        Sid    = "STSAccess"
        Effect = "Allow"
        Action = [
          "sts:AssumeRole",
          "sts:GetCallerIdentity"
        ]
        Resource = "*"
      }
    ]
  })
}
