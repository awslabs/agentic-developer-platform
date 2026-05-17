# =============================================================================
# Beads State — DynamoDB + S3 for agent issue tracking
# =============================================================================

resource "aws_dynamodb_table" "beads_manifest" {
  name         = "${var.name_prefix}-beads-manifest"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }

  tags = {
    Component = "agent-factory"
  }
}

resource "aws_s3_bucket" "beads_state" {
  bucket = "${var.name_prefix}-beads-state-${var.account_id}"

  tags = {
    Component = "agent-factory"
  }
}

resource "aws_s3_bucket_versioning" "beads_state" {
  bucket = aws_s3_bucket.beads_state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "beads_state" {
  bucket = aws_s3_bucket.beads_state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "beads_state" {
  bucket                  = aws_s3_bucket.beads_state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
