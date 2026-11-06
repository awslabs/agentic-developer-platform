terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  backend "local" {}
}

variable "account_id" { type = string }
variable "region" { type = string }
variable "environment" { type = string }
variable "gateway_role_name" { type = string }

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
}

resource "aws_s3_bucket" "snapshots" {
  bucket        = "adp-${var.environment}-product-source-snapshots-${var.account_id}"
  force_destroy = false
  tags          = { ManagedBy = "terraform", Purpose = "Product source snapshots", Environment = var.environment }
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_public_access_block" "snapshots" {
  bucket                  = aws_s3_bucket.snapshots.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "snapshots" {
  bucket = aws_s3_bucket.snapshots.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "snapshots" {
  bucket = aws_s3_bucket.snapshots.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_versioning" "snapshots" {
  bucket = aws_s3_bucket.snapshots.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_policy" "snapshots" {
  bucket = aws_s3_bucket.snapshots.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "RequireTLS", Effect = "Deny", Principal = "*", Action = "s3:*"
      Resource  = [aws_s3_bucket.snapshots.arn, "${aws_s3_bucket.snapshots.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false", "aws:PrincipalIsAWSService" = "false" } }
    }]
  })
}

resource "aws_iam_role_policy" "gateway" {
  name = "adp-${var.environment}-product-source-snapshots"
  role = var.gateway_role_name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "CaptureAndReadProductSources", Effect = "Allow"
      Action   = ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"]
      Resource = "${aws_s3_bucket.snapshots.arn}/products/source-snapshots/*"
    }]
  })
}

output "bucket_name" { value = aws_s3_bucket.snapshots.id }

resource "aws_ssm_parameter" "gateway_bucket" {
  name  = "/adp/${var.environment}/gateway/product-source-snapshot-bucket"
  type  = "String"
  value = aws_s3_bucket.snapshots.id
}
