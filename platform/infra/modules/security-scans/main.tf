# =============================================================================
# Security Scans — S3 bucket for SARIF archival
# =============================================================================
# Platform-level archival bucket for security scan results (SARIF files).
# Replaces broken GitHub Code Scanning uploads (GHAS not enabled).
# =============================================================================

locals {
  bucket_suffix = var.account_id != "" ? "-${var.account_id}" : ""
}

resource "aws_s3_bucket" "security_scans" {
  bucket = "adp-${var.environment}-security-scans${local.bucket_suffix}"
}

resource "aws_s3_bucket_versioning" "security_scans" {
  bucket = aws_s3_bucket.security_scans.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "security_scans" {
  bucket = aws_s3_bucket.security_scans.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "security_scans" {
  bucket                  = aws_s3_bucket.security_scans.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "security_scans" {
  bucket = aws_s3_bucket.security_scans.id
  rule {
    id     = "transition-and-expire"
    status = "Enabled"
    filter {}
    transition {
      days          = var.security_scan_glacier_days
      storage_class = "GLACIER"
    }
    expiration {
      days = var.security_scan_expire_days
    }
  }
}
