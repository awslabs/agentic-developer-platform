terraform {
  required_version = ">= 1.10"
  backend "s3" {}
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}
variable "account_id" { type = string }
variable "environment" {
  type    = string
  default = "dev"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "Use a deployment environment name."
  }
}
variable "region" {
  type    = string
  default = "us-east-1"
}
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
}
resource "aws_s3_bucket" "evidence" {
  bucket        = "adp-${var.environment}-ci-evidence-${var.account_id}"
  force_destroy = false
  tags          = { Project = "adp", Environment = var.environment, Purpose = "ci-evidence", ManagedBy = "terraform" }
}
resource "aws_s3_bucket_public_access_block" "evidence" {
  bucket                  = aws_s3_bucket.evidence.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_ownership_controls" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  rule { object_ownership = "BucketOwnerEnforced" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_versioning" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_lifecycle_configuration" "evidence" {
  bucket     = aws_s3_bucket.evidence.id
  depends_on = [aws_s3_bucket_versioning.evidence]
  dynamic "rule" {
    for_each = toset(["7", "14", "90"])
    content {
      id     = "retention-${rule.value}"
      status = "Enabled"
      filter { prefix = "artifacts/${rule.value}/" }
      expiration { days = tonumber(rule.value) }
      noncurrent_version_expiration { noncurrent_days = 1 }
      abort_incomplete_multipart_upload { days_after_initiation = 1 }
    }
  }
}
resource "aws_s3_bucket_policy" "evidence" {
  bucket     = aws_s3_bucket.evidence.id
  depends_on = [aws_s3_bucket_public_access_block.evidence]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "RequireTLS", Effect = "Deny", Principal = "*", Action = "s3:*",
        Resource  = [aws_s3_bucket.evidence.arn, "${aws_s3_bucket.evidence.arn}/*"],
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "RequireWriteOnce", Effect = "Deny", Principal = "*", Action = "s3:PutObject",
        Resource  = "${aws_s3_bucket.evidence.arn}/*",
        Condition = { StringNotEquals = { "s3:if-none-match" = "*" } }
      }
    ]
  })
}
output "bucket" { value = aws_s3_bucket.evidence.id }
