# Dedicated native source resources. Use one independent state per Product setup.
# Agent Space creation remains the original ADP operation, not a Terraform effect.
terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

provider "aws" {
  region              = var.selection.region
  allowed_account_ids = [var.selection.account_id]
}

locals {
  case_digest = substr(sha256(jsonencode([
    var.selection.tenant_id, var.selection.product_id,
    var.selection.project_id, var.selection.configuration_id
  ])), 0, 16)
  bucket_name = "adp-sa-${var.selection.account_id}-${var.selection.region}-${local.case_digest}"
  bucket_arn  = "arn:aws:s3:::${local.bucket_name}"
  role_name   = "ADP-Security-${local.case_digest}"
  role_arn    = "arn:aws:iam::${var.selection.account_id}:role/adp/security/${local.role_name}"
  space_arn   = "arn:aws:securityagent:${var.selection.region}:${var.selection.account_id}:agent-space/${var.selection.agent_space_id}"
  log_prefix  = "arn:aws:logs:${var.selection.region}:${var.selection.account_id}:log-group:/aws/securityagent/${var.selection.agent_space_name}"
  tags = {
    ManagedBy          = "terraform"
    Purpose            = "adp-security-native-assessment"
    "adp:case"         = local.case_digest
    "adp:tenant"       = var.selection.tenant_id
    "adp:source-scope" = var.selection.source_scope_sha256
  }
  trust = {
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "securityagent.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = var.selection.account_id }
        ArnEquals    = { "aws:SourceArn" = local.space_arn }
      }
    }]
  }
  execution_policy = {
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InspectOnlyThisServiceRole"
        Effect   = "Allow"
        Action   = ["iam:GetRole", "iam:SimulatePrincipalPolicy"]
        Resource = local.role_arn
      },
      {
        Sid       = "ReadSelectedSourceArchives"
        Effect    = "Allow"
        Action    = ["s3:GetObject"]
        Resource  = "${local.bucket_arn}/security-agent/source/*"
        Condition = { StringEquals = { "aws:ResourceAccount" = var.selection.account_id } }
      },
      {
        # Prefix-free native bucket preflight is allowed only in this dedicated bucket.
        Sid       = "ListOnlyThisCaseBucket"
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = local.bucket_arn
        Condition = { StringEquals = { "aws:ResourceAccount" = var.selection.account_id } }
      },
      {
        # AWS chooses per-assessment group names. Retention is a separate operator step.
        Sid       = "CreateOnlyThisSpaceLogs"
        Effect    = "Allow"
        Action    = ["logs:CreateLogGroup"]
        Resource  = "${local.log_prefix}*"
        Condition = { StringEquals = { "aws:ResourceAccount" = var.selection.account_id } }
      },
      {
        Sid       = "WriteOnlyThisSpaceLogs"
        Effect    = "Allow"
        Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource  = "${local.log_prefix}*:log-stream:${var.selection.agent_space_name}*"
        Condition = { StringEquals = { "aws:ResourceAccount" = var.selection.account_id } }
      }
    ]
  }
}

resource "aws_s3_bucket" "source" {
  bucket        = local.bucket_name
  force_destroy = false
  tags          = local.tags
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_ownership_controls" "source" {
  bucket = aws_s3_bucket.source.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_public_access_block" "source" {
  bucket                  = aws_s3_bucket.source.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "source" {
  bucket = aws_s3_bucket.source.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_versioning" "source" {
  bucket = aws_s3_bucket.source.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "source" {
  bucket     = aws_s3_bucket.source.id
  depends_on = [aws_s3_bucket_versioning.source]
  rule {
    id     = "expire-case-source-archives"
    status = "Enabled"
    filter { prefix = "security-agent/source/" }
    expiration { days = var.source_retention_days }
    noncurrent_version_expiration { noncurrent_days = var.source_retention_days }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}

resource "aws_iam_role" "execution" {
  name               = local.role_name
  path               = "/adp/security/"
  description        = "Static code review and document threat modeling for one reviewed ADP case"
  assume_role_policy = jsonencode(local.trust)
  tags               = local.tags
  lifecycle { prevent_destroy = true }
}

resource "aws_iam_role_policy" "execution" {
  name   = "selected-source-and-space-logs"
  role   = aws_iam_role.execution.id
  policy = jsonencode(local.execution_policy)
}

resource "aws_s3_bucket_policy" "source" {
  bucket = aws_s3_bucket.source.id
  # Wait for role creation and the public-access block; do not import a shared bucket.
  depends_on = [aws_iam_role.execution, aws_s3_bucket_public_access_block.source]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "RequireTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [local.bucket_arn, "${local.bucket_arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false", "aws:PrincipalIsAWSService" = "false" } }
      },
      {
        Sid       = "DenyUnselectedDataPrincipals"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:ListBucket", "s3:ListBucketVersions"]
        Resource  = [local.bucket_arn, "${local.bucket_arn}/*"]
        Condition = { ArnNotEquals = { "aws:PrincipalArn" = [local.role_arn, var.selection.connection_role_arn, var.selection.operator_role_arn] } }
      }
    ]
  })
}

output "review" {
  value = {
    source_bucket          = local.bucket_name
    execution_role_arn     = local.role_arn
    native_space_arn       = local.space_arn
    execution_trust        = local.trust
    execution_permissions  = local.execution_policy
    source_retention_days  = var.source_retention_days
    desired_log_retention  = var.log_retention_days
    log_retention_enforced = false
    registration_resources = {
      iamRoles  = [local.role_arn]
      s3Buckets = [local.bucket_arn]
    }
    # Use in existing provider-access root under a separate connection-owned state.
    provider_access_security = {
      enabled             = true
      source_bucket_names = [local.bucket_name]
      execution_role_arns = [local.role_arn]
    }
  }
}
