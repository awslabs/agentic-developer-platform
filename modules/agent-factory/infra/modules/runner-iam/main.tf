# =============================================================================
# Runner IAM — IRSA role for GitHub Actions runner pods
# =============================================================================
# Extracted from github-actions-runner/infrastructure/iam.tf
# Uses the shared EKS OIDC provider instead of creating a new one.
# =============================================================================

# Permissions boundary
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
          "ssm:*", "kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*",
          "iam:CreateRole", "iam:CreatePolicy", "iam:AttachRolePolicy",
          "iam:PutRolePolicy", "iam:PassRole", "iam:TagRole", "iam:TagPolicy",
          "iam:CreateServiceLinkedRole", "iam:GetRole", "iam:GetPolicy",
          "iam:GetRolePolicy", "iam:ListRolePolicies",
          "iam:ListRoles", "iam:ListPolicies", "iam:ListAttachedRolePolicies",
          "iam:DeleteRole", "iam:DeleteRolePolicy", "iam:DetachRolePolicy",
          "iam:UpdateAssumeRolePolicy",
          "iam:CreateInstanceProfile", "iam:AddRoleToInstanceProfile",
          "iam:DeleteInstanceProfile", "iam:RemoveRoleFromInstanceProfile",
          "iam:TagInstanceProfile",
          "sts:AssumeRole", "sts:GetCallerIdentity",
          "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
          "events:*", "stepfunctions:*",
          "cognito-idp:*", "cognito-identity:*",
          "elasticache:*", "eks:*",
          "sagemaker:*",
          # WAFv2 — needed by webhook-ingress module to protect API Gateway
          # with rate-limit Web ACLs. Scoped to actions Terraform actually uses.
          "wafv2:CreateWebACL", "wafv2:DeleteWebACL", "wafv2:UpdateWebACL",
          "wafv2:GetWebACL", "wafv2:ListWebACLs",
          "wafv2:AssociateWebACL", "wafv2:DisassociateWebACL",
          "wafv2:GetWebACLForResource", "wafv2:ListResourcesForWebACL",
          "wafv2:TagResource", "wafv2:UntagResource", "wafv2:ListTagsForResource"
        ]
        Resource = "*"
      },
      {
        # Self-manage boundary versions — required for Terraform to update the
        # boundary from a runner pod. Without this, updating the boundary
        # becomes a chicken-and-egg problem requiring admin intervention.
        Sid    = "ManageOwnBoundary"
        Effect = "Allow"
        Action = [
          "iam:CreatePolicyVersion",
          "iam:DeletePolicyVersion",
          "iam:SetDefaultPolicyVersion",
          "iam:ListPolicyVersions",
          "iam:GetPolicyVersion"
        ]
        Resource = "arn:aws:iam::${var.account_id}:policy/${var.name_prefix}-runner-boundary"
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
          "arn:aws:bedrock:*:${var.account_id}:inference-profile/*"
        ]
      },
      {
        Sid    = "SecretsManagerAccess"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret"
        ]
        Resource = "arn:aws:secretsmanager:${var.aws_region}:${var.account_id}:secret:adp/*"
      },
      {
        Sid      = "KMSAccess"
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        Resource = "*"
      },
      {
        Sid    = "IAMManagement"
        Effect = "Allow"
        Action = [
          "iam:CreateRole", "iam:CreatePolicy", "iam:AttachRolePolicy",
          "iam:PutRolePolicy", "iam:PassRole", "iam:TagRole", "iam:TagPolicy",
          "iam:CreateServiceLinkedRole", "iam:GetRole", "iam:GetPolicy",
          "iam:GetRolePolicy", "iam:ListRoles", "iam:ListPolicies",
          "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
          "iam:DeleteRole", "iam:DeleteRolePolicy", "iam:DetachRolePolicy",
          "iam:CreateInstanceProfile", "iam:AddRoleToInstanceProfile",
          "iam:DeleteInstanceProfile", "iam:RemoveRoleFromInstanceProfile",
          "iam:TagInstanceProfile"
        ]
        Resource = "*"
      },
      {
        Sid      = "STSAccess"
        Effect   = "Allow"
        Action   = ["sts:AssumeRole", "sts:GetCallerIdentity"]
        Resource = "*"
      },
      {
        Sid      = "ExecuteApiInvokeGateway"
        Effect   = "Allow"
        Action   = ["execute-api:Invoke"]
        Resource = "arn:aws:execute-api:${var.aws_region}:${var.account_id}:*/*/*/agent/*"
      }
    ]
  })
}
