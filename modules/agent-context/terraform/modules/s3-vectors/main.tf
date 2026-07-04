# =============================================================================
# S3 Vectors — Vector Bucket + Sharded Code Indexes
# =============================================================================
# Creates an S3 Vectors bucket with N hash-sharded indexes for code embeddings.
# Each index stores 1024-dim Titan Embed v2 vectors with cosine distance.
# Personal-context indexes are created dynamically per-user at runtime.
#
# NOTE: aws_s3vectors_* resources require AWS provider >= 5.101 (not yet
# released as of 2026-06-12). The bucket and indexes are provisioned via AWS
# CLI (scripts/ensure-vector-bucket.sh, invoked from deploy.sh) as an
# idempotent step. This module manages only the IAM policy attachment.
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

# S3 Vectors defines two resource types with these ARN formats
# (AWS Service Authorization Reference, service prefix "s3vectors"):
#   VectorBucket : arn:aws:s3vectors:<region>:<account>:bucket/<bucket-name>
#   Index        : arn:aws:s3vectors:<region>:<account>:bucket/<bucket-name>/index/<index-name>
# The earlier "vector-bucket/<name>" form was wrong and matched no request, so
# every call (CreateIndex, QueryVectors, PutVectors, ...) was denied. Actions
# are scoped to the resource type each one operates on.
data "aws_iam_policy_document" "s3_vectors" {
  # ListVectorBuckets is account-scoped — it takes no resource ARN, so it must
  # be granted on "*".
  statement {
    sid       = "S3VectorsListBuckets"
    effect    = "Allow"
    actions   = ["s3vectors:ListVectorBuckets"]
    resources = ["*"]
  }

  # Bucket-level actions operate on the VectorBucket resource type.
  statement {
    sid    = "S3VectorsBucket"
    effect = "Allow"
    actions = [
      "s3vectors:CreateVectorBucket",
      "s3vectors:DeleteVectorBucket",
      "s3vectors:GetVectorBucket",
      "s3vectors:ListIndexes",
    ]
    resources = [
      "arn:aws:s3vectors:${var.aws_region}:${var.account_id}:bucket/adp-*",
    ]
  }

  # Index-level actions operate on the Index resource type (bucket/<name>/index/<name>).
  # CreateIndex targets the Index being created, so it is scoped here (not to the bucket).
  statement {
    sid    = "S3VectorsIndex"
    effect = "Allow"
    actions = [
      "s3vectors:CreateIndex",
      "s3vectors:DeleteIndex",
      "s3vectors:GetIndex",
      "s3vectors:PutVectors",
      "s3vectors:GetVectors",
      "s3vectors:DeleteVectors",
      "s3vectors:ListVectors",
      "s3vectors:QueryVectors",
    ]
    resources = [
      "arn:aws:s3vectors:${var.aws_region}:${var.account_id}:bucket/adp-*/index/*",
    ]
  }
}

resource "aws_iam_role_policy" "s3_vectors" {
  name   = "${var.name_prefix}-s3-vectors"
  role   = var.irsa_role_name
  policy = data.aws_iam_policy_document.s3_vectors.json
}
