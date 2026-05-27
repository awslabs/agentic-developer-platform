# =============================================================================
# Security Scans — S3 bucket for SARIF archival
# =============================================================================
# Platform-level archival bucket for security scan results (SARIF files).
# Replaces broken GitHub Code Scanning uploads (GHAS not enabled).
# =============================================================================

resource "aws_s3_bucket" "security_scans" {
  # The account-ID suffix from PR #715 was never applied — bucket existed pre-PR
  # as `adp-<env>-security-scans` and S3 buckets cannot be renamed in place.
  # Every Platform Infra Apply since #715 has planned a destroy+recreate that
  # fails (BucketNotEmpty), causing partial damage to the bucket's protection
  # rules. Pin to the actual existing name; account-ID isolation is sufficient
  # to avoid global-namespace collisions because each account has only one
  # adp-<env>-security-scans bucket.
  bucket = "adp-${var.environment}-security-scans"
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
