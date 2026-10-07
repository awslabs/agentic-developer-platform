# Evidence bodies live in S3; GitHub OIDC signatures bind their exact bytes to
# repository/run/attempt/workflow. No state-file or general bucket reader grant.
terraform {
  required_providers { aws = { source = "hashicorp/aws", version = "~> 6.0" } }
}
variable "account_id" { type = string }
variable "environment" { type = string }
variable "writer_roles" { type = set(string) }
variable "reader_role" { type = string }

locals {
  bucket = "adp-${var.environment}-deployment-evidence-${var.account_id}"
  prefix = "deployment-evidence/v1/"
}
resource "aws_s3_bucket" "evidence" {
  bucket = local.bucket
  tags   = { Project = "adp", Environment = var.environment, Component = "gateway-deployment-evidence", ManagedBy = "terraform" }
  lifecycle { prevent_destroy = true }
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
resource "aws_s3_bucket_versioning" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_lifecycle_configuration" "evidence" {
  bucket     = aws_s3_bucket.evidence.id
  depends_on = [aws_s3_bucket_versioning.evidence]
  rule {
    id     = "deployment-evidence-retention"
    status = "Enabled"
    filter { prefix = local.prefix }
    expiration { days = 30 }
    noncurrent_version_expiration { noncurrent_days = 30 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}
resource "aws_s3_bucket_policy" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "TLSOnly", Effect = "Deny", Principal = "*", Action = "s3:*", Resource = [aws_s3_bucket.evidence.arn, "${aws_s3_bucket.evidence.arn}/*"], Condition = { Bool = { "aws:SecureTransport" = "false" } } },
    { Sid = "ConditionalCreationOnly", Effect = "Deny", Principal = "*", Action = "s3:PutObject", Resource = "${aws_s3_bucket.evidence.arn}/${local.prefix}*", Condition = { StringNotEquals = { "s3:if-none-match" = "*" } } },
    { Sid = "WritersCannotDeleteEvidence", Effect = "Deny", Principal = "*", Action = ["s3:DeleteObject", "s3:DeleteObjectVersion"], Resource = "${aws_s3_bucket.evidence.arn}/${local.prefix}*", Condition = { ArnEquals = { "aws:PrincipalArn" = [for role in var.writer_roles : "arn:aws:iam::${var.account_id}:role/${role}"] } } },
  ] })
}
resource "aws_iam_policy" "write" {
  name = "adp-${var.environment}-deployment-evidence-write"
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "s3:PutObject", Resource = "${aws_s3_bucket.evidence.arn}/${local.prefix}*", Condition = { StringEquals = { "s3:if-none-match" = "*", "s3:x-amz-server-side-encryption" = "AES256" } } },
  ] })
}
resource "aws_iam_role_policy_attachment" "write" {
  for_each   = var.writer_roles
  role       = each.value
  policy_arn = aws_iam_policy.write.arn
}
resource "aws_iam_policy" "read" {
  name = "adp-${var.environment}-deployment-evidence-read"
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "${aws_s3_bucket.evidence.arn}/${local.prefix}*" },
    { Effect = "Allow", Action = "s3:ListBucket", Resource = aws_s3_bucket.evidence.arn, Condition = { StringLike = { "s3:prefix" = "${local.prefix}*" } } },
  ] })
}
resource "aws_iam_role_policy_attachment" "read" {
  role       = var.reader_role
  policy_arn = aws_iam_policy.read.arn
}
output "bucket_name" { value = aws_s3_bucket.evidence.id }
output "objects_arn" { value = "${aws_s3_bucket.evidence.arn}/${local.prefix}*" }
