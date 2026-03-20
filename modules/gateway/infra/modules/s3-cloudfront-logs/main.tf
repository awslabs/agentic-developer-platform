# S3 bucket for CloudFront standard access logs
# CloudFront standard logging requires ACL-based permissions (not bucket policy).

resource "aws_s3_bucket" "cloudfront_logs" {
  bucket = "${var.name_prefix}-cloudfront-logs-${data.aws_caller_identity.current.account_id}"

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-cloudfront-logs"
    Service = "s3"
    Purpose = "cloudfront-access-logs"
  })
}

# CloudFront standard logging requires BucketOwnerPreferred ownership
resource "aws_s3_bucket_ownership_controls" "cloudfront_logs" {
  bucket = aws_s3_bucket.cloudfront_logs.id

  rule {
    object_ownership = "BucketOwnerPreferred"
  }
}

# Enable ACLs — CloudFront writes logs via the awslogsdelivery canonical user
resource "aws_s3_bucket_acl" "cloudfront_logs" {
  bucket     = aws_s3_bucket.cloudfront_logs.id
  acl        = "log-delivery-write"
  depends_on = [aws_s3_bucket_ownership_controls.cloudfront_logs]
}

# Block public access
resource "aws_s3_bucket_public_access_block" "cloudfront_logs" {
  bucket = aws_s3_bucket.cloudfront_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Lifecycle rule — expire logs after retention period
resource "aws_s3_bucket_lifecycle_configuration" "cloudfront_logs" {
  bucket = aws_s3_bucket.cloudfront_logs.id

  rule {
    id     = "expire-old-logs"
    status = "Enabled"

    expiration {
      days = var.log_retention_days
    }
  }
}

# Server-side encryption
resource "aws_s3_bucket_server_side_encryption_configuration" "cloudfront_logs" {
  bucket = aws_s3_bucket.cloudfront_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

data "aws_caller_identity" "current" {}
