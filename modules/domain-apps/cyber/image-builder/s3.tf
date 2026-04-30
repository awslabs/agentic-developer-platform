# =============================================================================
# Phase 1: S3 Assets Bucket
# =============================================================================
# Stores built qcow2 images and checksums.
# Versioning enabled so rebuilds produce new versions without overwriting.
# =============================================================================

# Import blocks allow Terraform to adopt pre-existing bucket resources
# (the bucket may have been created by earlier manual runs or other modules).
import {
  to = aws_s3_bucket.cape_assets
  id = var.assets_bucket_name
}

import {
  to = aws_s3_bucket_versioning.cape_assets
  id = var.assets_bucket_name
}

import {
  to = aws_s3_bucket_server_side_encryption_configuration.cape_assets
  id = var.assets_bucket_name
}

import {
  to = aws_s3_bucket_public_access_block.cape_assets
  id = var.assets_bucket_name
}

import {
  to = aws_s3_bucket_lifecycle_configuration.cape_assets
  id = var.assets_bucket_name
}

resource "aws_s3_bucket" "cape_assets" {
  bucket = var.assets_bucket_name

  tags = {
    Name      = var.assets_bucket_name
    Component = "cyber-sandbox"
  }
}

resource "aws_s3_bucket_versioning" "cape_assets" {
  bucket = aws_s3_bucket.cape_assets.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cape_assets" {
  bucket = aws_s3_bucket.cape_assets.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "cape_assets" {
  bucket = aws_s3_bucket.cape_assets.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "cape_assets" {
  bucket = aws_s3_bucket.cape_assets.id

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}
