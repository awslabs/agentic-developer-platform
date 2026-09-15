# Data source for current AWS account
data "aws_caller_identity" "current" {}

# Local values for bucket naming
locals {
  # Shorten name_prefix by removing "bedrockgw-" prefix if present to stay under 63 chars
  short_prefix = replace(var.name_prefix, "bedrockgw-", "bg-")
  # Account ID last 8 chars to keep bucket name short
  short_account_id = substr(data.aws_caller_identity.current.account_id, -8, 8)
}

# S3 Bucket for Frontend SPA
resource "aws_s3_bucket" "frontend" {
  bucket        = "${var.name_prefix}-frontend-${local.short_account_id}"
  force_destroy = var.environment != "prod"

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-frontend"
    Service = "frontend"
    Purpose = "spa-hosting"
  })

  # Provider 6.x still carries deprecated inline policy/acl/grant/cors_rule/
  # lifecycle_rule/versioning on aws_s3_bucket as optional+computed, and this
  # state holds them null or empty because each concern is owned by its own
  # dedicated aws_s3_bucket_* resource below. The consequence is that ANY planned
  # update to this resource — a tag edit, a force_destroy change, anything — makes
  # the SDK's update path push those empties to S3.
  #
  # This is not theoretical. On 2026-08-31T20:35Z a single planned update here
  # issued DeleteBucketPolicy, DeleteBucketCors, DeleteBucketLifecycle and
  # PutBucketVersioning(Suspended) against the live bucket, confirmed in
  # CloudTrail. Deleting the policy removed CloudFront's OAC read grant and took
  # the dashboard down until the policy was reapplied.
  #
  # ignore_changes suppresses the diff, so no update is planned, so those calls
  # are never issued. `grant` is included because state holds a CanonicalUser
  # FULL_CONTROL grant that the config does not declare — another latent diff on
  # the same resource.
  lifecycle {
    ignore_changes = [policy, acl, grant, cors_rule, lifecycle_rule, versioning]
  }
}

# Block ALL public access (CloudFront uses OAC, not public bucket)
resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Enable versioning
resource "aws_s3_bucket_versioning" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Server-side encryption with SSE-S3 (AES256)
resource "aws_s3_bucket_server_side_encryption_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Lifecycle rule: delete non-current versions after 30 days
resource "aws_s3_bucket_lifecycle_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  rule {
    id     = "delete_noncurrent_versions"
    status = "Enabled"

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_expiration_days
    }

    # Clean up incomplete multipart uploads after 7 days
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Bucket policy: allow CloudFront OAC to GetObject
resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowCloudFrontOAC"
        Effect = "Allow"
        Principal = {
          Service = "cloudfront.amazonaws.com"
        }
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.frontend.arn}/*"
        Condition = {
          StringEquals = {
            "AWS:SourceArn" = var.cloudfront_distribution_arn
          }
        }
      }
    ]
  })

  # Ensure public access block is applied first
  depends_on = [aws_s3_bucket_public_access_block.frontend]
}

# CORS configuration allowing GET from the gateway domain
resource "aws_s3_bucket_cors_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["GET", "HEAD"]
    allowed_origins = var.cors_allowed_origins
    expose_headers  = ["ETag"]
    max_age_seconds = 3600
  }
}
