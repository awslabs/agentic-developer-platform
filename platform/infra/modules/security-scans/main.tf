# =============================================================================
# Security Scans — S3 bucket for SARIF archival
# =============================================================================
# Platform-level archival bucket for security scan results (SARIF files).
# Replaces broken GitHub Code Scanning uploads (GHAS not enabled).
# =============================================================================

resource "aws_s3_bucket" "security_scans" {
  # Account-ID suffix ensures global uniqueness across AWS accounts.
  # Migration note (issue #982): the platform account's legacy bare-named bucket
  # (adp-dev-security-scans) must be deleted or emptied before applying this
  # change there — S3 buckets cannot be renamed in place. New/cross-account
  # deploys get the suffixed name on first apply with no migration needed.
  bucket = "adp-${var.environment}-security-scans-${var.account_id}"

  # force_destroy lets Terraform empty the bucket itself when it must be
  # destroyed/replaced. Required for the #982 rename: the legacy bucket is
  # continuously written to by running CI security scans (SARIF/SBOM uploads),
  # so manual "empty then apply" always loses the race — a new object lands in
  # the gap and DeleteBucket fails with BucketNotEmpty. Terraform empties +
  # deletes atomically with retries, closing the window. Safe here: contents
  # are disposable scan artifacts (regenerated every scan; lifecycle-expired
  # anyway), never source-of-truth data.
  force_destroy = true
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
