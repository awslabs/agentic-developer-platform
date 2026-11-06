resource "aws_kms_key" "evidence" {
  description             = "Engineering Insights evidence lake"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = { Module = "insights", Environment = var.environment }
}

resource "aws_kms_alias" "evidence" {
  name          = "alias/${local.prefix}-evidence"
  target_key_id = aws_kms_key.evidence.id
}

resource "aws_s3_bucket" "evidence" {
  bucket        = "${local.prefix}-evidence"
  force_destroy = false
  tags          = { Module = "insights", Layout = "assurance/v1/" }
}

resource "aws_s3_bucket_versioning" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.evidence.arn
    }
    bucket_key_enabled = true
  }
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

resource "aws_s3_bucket_policy" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "RequireTLS"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.evidence.arn, "${aws_s3_bucket.evidence.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.evidence]
}

resource "aws_ecr_repository" "services" {
  for_each             = toset(["emitter", "distiller"])
  name                 = "${local.prefix}-${each.key}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

output "evidence_bucket" { value = aws_s3_bucket.evidence.id }
output "emitter_image" { value = var.emitter_image }
output "distiller_image" { value = var.distiller_image }
output "ecr_repositories" { value = { for name, repository in aws_ecr_repository.services : name => repository.repository_url } }

output "release_configuration" {
  value = {
    aws_account_id   = var.aws_account_id
    emitter_image    = var.emitter_image
    distiller_image  = var.distiller_image
    github_api_cidrs = var.github_api_cidrs
  }
}
