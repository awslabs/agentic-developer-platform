terraform {
  required_version = ">= 1.14"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  backend "s3" {}
}

provider "aws" {
  region              = "us-east-1"
  allowed_account_ids = [local.accounts[var.environment]]
}

variable "environment" {
  type = string
  validation {
    condition     = contains(["integration-test", "pre-production"], var.environment)
    error_message = "Choose integration-test or pre-production."
  }
}

variable "existing_oidc_provider_arn" {
  type    = string
  default = ""
}

locals {
  accounts = { integration-test = "608380991969", pre-production = "615296308642" }
  account  = local.accounts[var.environment]
  build    = var.environment == "integration-test"
  bucket   = "adp-release-artifacts-608380991969"
  provider = var.existing_oidc_provider_arn != "" ? var.existing_oidc_provider_arn : aws_iam_openid_connect_provider.github[0].arn
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.existing_oidc_provider_arn == "" ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_iam_role" "deploy" {
  name                 = "adp-release-deploy"
  max_session_duration = 14400
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Principal = { Federated = local.provider }, Action = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "repo:aws-e/adp:environment:${var.environment}"
      } }
    }]
  })
}

# Full platform Terraform manages IAM, EKS, networking and credentials. This is
# deliberately an administrator role, isolated by account and GitHub environment;
# it must not be described as a least-privilege application deployment role.
resource "aws_iam_role_policy_attachment" "deploy" {
  role       = aws_iam_role.deploy.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# Owned by release-infra, so the very first CI upgrade can authenticate to EKS
# before planning the existing Kubernetes resources in platform state.
resource "aws_eks_access_entry" "deploy" {
  cluster_name  = "adp-dev-eks-cluster"
  principal_arn = aws_iam_role.deploy.arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "deploy" {
  cluster_name  = aws_eks_access_entry.deploy.cluster_name
  principal_arn = aws_eks_access_entry.deploy.principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope { type = "cluster" }
}

resource "aws_iam_role" "build" {
  count                = local.build ? 1 : 0
  name                 = "adp-release-build"
  max_session_duration = 14400
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Principal = { Federated = local.provider }, Action = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "repo:aws-e/adp:environment:adp-release-build"
      } }
    }]
  })
}

resource "aws_iam_role_policy" "build" {
  count = local.build ? 1 : 0
  role  = aws_iam_role.build[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["codebuild:StartBuild", "codebuild:BatchGetBuilds"],
        Resource = concat(
          [for name in ["gateway-build", "agent-runtime", "agent-gateway", "chat-agent"] : "arn:aws:codebuild:us-east-1:${local.account}:project/adp-dev-${name}"],
          [for name in ["gateway-build", "agent-runtime", "agent-gateway", "chat-agent"] : "arn:aws:codebuild:us-east-1:${local.account}:build/adp-dev-${name}:*"]
      ) },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"],
      Resource = ["arn:aws:s3:::${local.bucket}/*", "arn:aws:s3:::adp-terraform-state-${local.account}/codebuild/src/*"] },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = ["arn:aws:s3:::${local.bucket}"] },
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
      { Effect = "Allow", Action = ["ecr:DescribeImages", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
      Resource = [for name in ["adp-gateway", "adp-agent-runtime", "adp-agent-gateway", "adp-chat-agent"] : "arn:aws:ecr:us-east-1:${local.account}:repository/${name}"] }
    ]
  })
}

resource "aws_s3_bucket" "releases" {
  count  = local.build ? 1 : 0
  bucket = local.bucket
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_versioning" "releases" {
  count  = local.build ? 1 : 0
  bucket = aws_s3_bucket.releases[0].id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_public_access_block" "releases" {
  count                   = local.build ? 1 : 0
  bucket                  = aws_s3_bucket.releases[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "releases" {
  count  = local.build ? 1 : 0
  bucket = aws_s3_bucket.releases[0].id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_policy" "releases" {
  count  = local.build ? 1 : 0
  bucket = aws_s3_bucket.releases[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid      = "TLS", Effect = "Deny", Principal = "*", Action = "s3:*",
        Resource = ["arn:aws:s3:::${local.bucket}", "arn:aws:s3:::${local.bucket}/*"],
      Condition = { Bool = { "aws:SecureTransport" = "false" } } },
      { Sid      = "ConditionalCreationOnly", Effect = "Deny", Principal = "*", Action = "s3:PutObject",
        Resource = "arn:aws:s3:::${local.bucket}/*",
      Condition = { StringNotEquals = { "s3:if-none-match" = "*" } } },
      { Sid = "NoDeletion", Effect = "Deny", Principal = "*", Action = ["s3:DeleteObject", "s3:DeleteObjectVersion"],
      Resource = "arn:aws:s3:::${local.bucket}/*" },
      { Sid      = "PromotionRead", Effect = "Allow", Principal = { AWS = "arn:aws:iam::615296308642:root" }, Action = "s3:GetObject",
        Resource = "arn:aws:s3:::${local.bucket}/*",
      Condition = { ArnEquals = { "aws:PrincipalArn" = "arn:aws:iam::615296308642:role/adp-release-deploy" } } }
    ]
  })
}

output "deploy_role_arn" { value = aws_iam_role.deploy.arn }
output "build_role_arn" { value = local.build ? aws_iam_role.build[0].arn : null }
