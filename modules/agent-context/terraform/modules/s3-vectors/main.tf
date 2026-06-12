# =============================================================================
# S3 Vectors — Vector Bucket + Sharded Code Indexes
# =============================================================================
# Creates an S3 Vectors bucket with N hash-sharded indexes for code embeddings.
# Each index stores 1024-dim Titan Embed v2 vectors with cosine distance.
# Personal-context indexes are created dynamically per-user at runtime.
#
# NOTE: aws_s3vectors_* resources require AWS provider >= 5.101 (not yet
# released as of 2026-06-12). The bucket and indexes are created via AWS CLI
# as a manual step. This module manages only the IAM policy attachment.
# When provider support lands, uncomment the resources below.
# =============================================================================

# resource "aws_s3vectors_vector_bucket" "code_vectors" {
#   vector_bucket_name = "adp-${var.environment}-code-vectors-${var.account_id}"
# }
#
# resource "aws_s3vectors_index" "code_shards" {
#   count = var.shard_count
#
#   vector_bucket_name = aws_s3vectors_vector_bucket.code_vectors.vector_bucket_name
#   index_name         = "code-shard-${count.index}"
#
#   dimension       = var.dimension
#   distance_metric = var.distance_metric
#   data_type       = "float32"
# }

locals {
  # Constructed bucket name (mirrors what the resource would create)
  vector_bucket_name = "adp-${var.environment}-code-vectors-${var.account_id}"
}

# =============================================================================
# IAM Policy for S3 Vectors access
# =============================================================================

data "aws_iam_policy_document" "s3_vectors" {
  statement {
    sid    = "S3VectorsAccess"
    effect = "Allow"
    actions = [
      "s3vectors:CreateVectorBucket",
      "s3vectors:DeleteVectorBucket",
      "s3vectors:GetVectorBucket",
      "s3vectors:ListVectorBuckets",
      "s3vectors:CreateIndex",
      "s3vectors:DeleteIndex",
      "s3vectors:GetIndex",
      "s3vectors:ListIndexes",
      "s3vectors:PutVectors",
      "s3vectors:GetVectors",
      "s3vectors:DeleteVectors",
      "s3vectors:ListVectors",
      "s3vectors:QueryVectors",
    ]
    resources = [
      "arn:aws:s3vectors:${var.aws_region}:${var.account_id}:vector-bucket/adp-*",
    ]
  }
}

resource "aws_iam_role_policy" "s3_vectors" {
  name   = "${var.name_prefix}-s3-vectors"
  role   = var.irsa_role_name
  policy = data.aws_iam_policy_document.s3_vectors.json
}
