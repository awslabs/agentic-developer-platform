# =============================================================================
# S3 Bucket for Chat Logs (Issue #143)
# =============================================================================
# Creates an S3 bucket for storing async chat logs with:
# - SSE-S3 or SSE-KMS encryption
# - Public access blocked
# - Lifecycle policy: Glacier after 90 days, delete after 365 days
# - Versioning for audit compliance
# =============================================================================

# S3 Bucket
resource "aws_s3_bucket" "chat_logs" {
  bucket = "${var.name_prefix}-chat-logs"

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-chat-logs"
    Service = "s3"
    Purpose = "chat-logs"
  })
}

# Enable versioning for audit compliance
resource "aws_s3_bucket_versioning" "chat_logs" {
  bucket = aws_s3_bucket.chat_logs.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Block all public access
resource "aws_s3_bucket_public_access_block" "chat_logs" {
  bucket = aws_s3_bucket.chat_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Server-side encryption
resource "aws_s3_bucket_server_side_encryption_configuration" "chat_logs" {
  bucket = aws_s3_bucket.chat_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.kms_key_arn != "" ? "aws:kms" : "AES256"
      kms_master_key_id = var.kms_key_arn != "" ? var.kms_key_arn : null
    }
    bucket_key_enabled = var.kms_key_arn != "" ? true : false
  }
}

# Lifecycle policy: Glacier after 90 days, delete after 365 days
resource "aws_s3_bucket_lifecycle_configuration" "chat_logs" {
  bucket = aws_s3_bucket.chat_logs.id

  rule {
    id     = "chat-logs-lifecycle"
    status = "Enabled"

    # Transition to Glacier after 90 days
    transition {
      days          = 90
      storage_class = "GLACIER"
    }

    # Delete after 365 days
    expiration {
      days = 365
    }

    # Clean up noncurrent versions
    noncurrent_version_transition {
      noncurrent_days = 30
      storage_class   = "GLACIER"
    }

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }

  # Abort incomplete multipart uploads after 7 days
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Bucket policy to enforce TLS and restrict access
resource "aws_s3_bucket_policy" "chat_logs" {
  bucket = aws_s3_bucket.chat_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnforceTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.chat_logs.arn,
          "${aws_s3_bucket.chat_logs.arn}/*"
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      }
    ]
  })
}

# Enable logging if log bucket is provided
resource "aws_s3_bucket_logging" "chat_logs" {
  count = var.log_bucket_name != "" ? 1 : 0

  bucket        = aws_s3_bucket.chat_logs.id
  target_bucket = var.log_bucket_name
  target_prefix = "s3-access-logs/${var.name_prefix}-chat-logs/"
}
