# =============================================================================
# GitLab CE Infrastructure — S3 Backup Bucket
# =============================================================================
# Daily backup storage with lifecycle policies:
# - Standard tier: 0-30 days
# - Glacier transition: after 30 days
# - Deletion: after 90 days (configurable via var.backup_retention_days)
# =============================================================================

resource "aws_s3_bucket" "backup" {
  count = var.backup_enabled ? 1 : 0

  bucket = "${local.name_prefix}-backups-${data.aws_caller_identity.current.account_id}"

  tags = merge(local.common_tags, {
    Name      = "${local.name_prefix}-backups"
    Service   = "storage"
    Component = "backup"
    Purpose   = "GitLab daily backups"
  })
}

# -----------------------------------------------------------------------------
# Versioning — protects against accidental overwrites
# -----------------------------------------------------------------------------

resource "aws_s3_bucket_versioning" "backup" {
  count = var.backup_enabled ? 1 : 0

  bucket = aws_s3_bucket.backup[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

# -----------------------------------------------------------------------------
# Server-side encryption (AES-256)
# -----------------------------------------------------------------------------

resource "aws_s3_bucket_server_side_encryption_configuration" "backup" {
  count = var.backup_enabled ? 1 : 0

  bucket = aws_s3_bucket.backup[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# -----------------------------------------------------------------------------
# Lifecycle rules — Glacier after 30 days, delete after retention_days
# -----------------------------------------------------------------------------

resource "aws_s3_bucket_lifecycle_configuration" "backup" {
  count = var.backup_enabled ? 1 : 0

  bucket = aws_s3_bucket.backup[0].id

  rule {
    id     = "glacier-transition"
    status = "Enabled"

    filter {
      prefix = "daily/"
    }

    transition {
      days          = 30
      storage_class = "GLACIER"
    }

    expiration {
      days = var.backup_retention_days
    }
  }

  rule {
    id     = "config-lifecycle"
    status = "Enabled"

    filter {
      prefix = "config/"
    }

    transition {
      days          = 30
      storage_class = "GLACIER"
    }

    expiration {
      days = var.backup_retention_days
    }
  }

  # Clean up incomplete multipart uploads after 7 days
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# -----------------------------------------------------------------------------
# Block all public access
# -----------------------------------------------------------------------------

resource "aws_s3_bucket_public_access_block" "backup" {
  count = var.backup_enabled ? 1 : 0

  bucket = aws_s3_bucket.backup[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
