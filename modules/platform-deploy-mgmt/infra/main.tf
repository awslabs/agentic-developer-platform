# =============================================================================
# Platform Deploy Management Infrastructure
# =============================================================================
# Evidence storage (S3), deployment status tracking (DynamoDB), and IRSA role
# for the verification workflow. Lives in the platform account (879318057152).
# =============================================================================

terraform {
  required_version = ">= 1.5"

  backend "s3" {
    # Configured via -backend-config during terraform init
    # See environments/dev/modules/platform-deploy-mgmt-backend.tfvars
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "adp-platform-deploy-mgmt"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  name_prefix = "adp-platform-deploy"
  account_id  = data.aws_caller_identity.current.account_id
}

# =============================================================================
# S3 Bucket: Verification Evidence
# =============================================================================
# Stores JSON evidence files from each phase verification run.
# Versioned, encrypted, no public access, lifecycle to Glacier after 90d.
# Deletion is explicitly denied (audit trail must persist).
# =============================================================================

resource "aws_s3_bucket" "evidence" {
  bucket = "adp-platform-deploy-evidence"

  # Prevent accidental destruction
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  rule {
    id     = "archive-to-glacier"
    status = "Enabled"

    filter {} # Apply to all objects in the bucket

    transition {
      days          = 90
      storage_class = "GLACIER"
    }

    # No expiration — deletion is forbidden for audit trail
  }
}

resource "aws_s3_bucket_policy" "evidence_deny_delete" {
  bucket = aws_s3_bucket.evidence.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyDeleteExceptBreakGlass"
        Effect    = "Deny"
        Principal = "*"
        Action = [
          "s3:DeleteObject",
          "s3:DeleteObjectVersion",
          "s3:DeleteBucket"
        ]
        Resource = [
          aws_s3_bucket.evidence.arn,
          "${aws_s3_bucket.evidence.arn}/*"
        ]
        Condition = {
          StringNotEquals = {
            "aws:PrincipalArn" = "arn:aws:iam::${local.account_id}:role/adp-break-glass-admin"
          }
        }
      }
    ]
  })
}

# =============================================================================
# DynamoDB Table: Deployment Status Tracking
# =============================================================================
# Tracks per-customer, per-phase deployment status. Used by monitoring,
# upgrade-diff, and the orchestrator to gate progression.
# =============================================================================

resource "aws_dynamodb_table" "deployments" {
  name         = "adp-platform-deployments"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "deployment_id"
  range_key    = "phase"

  attribute {
    name = "deployment_id"
    type = "S"
  }

  attribute {
    name = "phase"
    type = "N"
  }

  attribute {
    name = "customer_account_id"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "last_check_passed_at"
    type = "S"
  }

  global_secondary_index {
    name            = "customer_account_id-index"
    hash_key        = "customer_account_id"
    projection_type = "ALL"
  }

  global_secondary_index {
    name            = "health-index"
    hash_key        = "status"
    range_key       = "last_check_passed_at"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

# =============================================================================
# IAM Role: Verify Workflow (IRSA)
# =============================================================================
# Assumed by the ARC runner via IRSA for the verify workflow.
# Permissions: write evidence to S3, update DDB, assume customer roles.
# =============================================================================

data "terraform_remote_state" "platform" {
  backend = "s3"
  config = {
    bucket = "adp-terraform-state-${local.account_id}"
    key    = "${var.environment}/platform/terraform.tfstate"
    region = var.aws_region
  }
}

resource "aws_iam_role" "verify_workflow" {
  name = "${local.name_prefix}-verify-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = data.terraform_remote_state.platform.outputs.eks_oidc_provider_arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${replace(data.terraform_remote_state.platform.outputs.eks_oidc_issuer, "https://", "")}:sub" = "system:serviceaccount:arc-runners:github-runner-sa"
            "${replace(data.terraform_remote_state.platform.outputs.eks_oidc_issuer, "https://", "")}:aud" = "sts.amazonaws.com"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "verify_workflow_permissions" {
  name = "${local.name_prefix}-verify-permissions"
  role = aws_iam_role.verify_workflow.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "WriteEvidence"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:PutObjectTagging"
        ]
        Resource = "${aws_s3_bucket.evidence.arn}/*"
      },
      {
        Sid    = "UpdateDeploymentStatus"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:GetItem",
          "dynamodb:Query"
        ]
        Resource = [
          aws_dynamodb_table.deployments.arn,
          "${aws_dynamodb_table.deployments.arn}/index/*"
        ]
      },
      {
        Sid      = "AssumeCustomerRole"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = "arn:aws:iam::*:role/adp-customer-verify-role"
        Condition = {
          StringEquals = {
            "sts:ExternalId" = "adp-platform-verify"
          }
        }
      }
    ]
  })
}
