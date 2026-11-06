terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  bucket_arn = "arn:${data.aws_partition.current.partition}:s3:::${var.bucket_name}"
  # Deliberately literal IAM policy variables, expanded by AWS at session use.
  product_prefix = "tenants/$${aws:PrincipalTag/adp_tenant_id}/products/$${aws:PrincipalTag/adp_product_id}/"
  kms_via_s3     = "s3.${data.aws_region.current.name}.${data.aws_partition.current.dns_suffix}"
}

resource "aws_kms_key" "shared" {
  description             = "Shared encryption for Product Context objects"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = merge(var.tags, { Service = "product-context" })
  lifecycle { prevent_destroy = true }
}

resource "aws_kms_alias" "shared" {
  name          = "alias/${var.alias_prefix}/shared"
  target_key_id = aws_kms_key.shared.key_id
}

resource "aws_kms_key" "product" {
  for_each                = var.managed_product_keys
  description             = "Dedicated encryption for Product Context ${each.key}"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags = merge(var.tags, {
    Service = "product-context", "adp:tenant_id" = each.value.tenant_id, "adp:product_id" = each.value.product_id
  })
  lifecycle { prevent_destroy = true }
}

resource "aws_kms_alias" "product" {
  for_each      = var.managed_product_keys
  name          = "alias/${var.alias_prefix}/${sha256("${each.value.tenant_id}/${each.value.product_id}")}"
  target_key_id = aws_kms_key.product[each.key].key_id
}

resource "aws_iam_role" "product_session" {
  name = "${var.name_prefix}-context-product-session"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Principal = { AWS = var.gateway_role_arn }
      Action = ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"]
      Condition = {
        Null                        = { "aws:RequestTag/adp_tenant_id" = "false", "aws:RequestTag/adp_product_id" = "false" }
        "ForAllValues:StringEquals" = { "aws:TagKeys" = ["adp_tenant_id", "adp_product_id"] }
        StringEquals                = { "sts:SourceIdentity" = "knowledge-ingestion" }
      }
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "product_session" {
  role = aws_iam_role.product_session.id
  name = "product-prefix-and-encryption"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]
        Resource = "${local.bucket_arn}/${local.product_prefix}*"
      },
      {
        Effect    = "Allow", Action = ["s3:ListBucket"], Resource = local.bucket_arn
        Condition = { StringLike = { "s3:prefix" = "${local.product_prefix}*" } }
      },
      {
        Effect   = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = "arn:${data.aws_partition.current.partition}:kms:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:key/*"
        # The per-request session policy narrows this to the exact selected key.
        # ViaService prevents direct KMS operations with these credentials.
        Condition = {
          StringEquals = { "kms:ViaService" = local.kms_via_s3 }
          StringLike   = { "kms:EncryptionContext:aws:s3:arn" = [local.bucket_arn, "${local.bucket_arn}/${local.product_prefix}*"] }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "gateway_broker" {
  role = var.gateway_role_name
  name = "${var.name_prefix}-context-storage-broker"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"], Resource = aws_iam_role.product_session.arn },
      {
        Effect   = "Allow", Action = ["kms:DescribeKey", "kms:ListResourceTags"]
        Resource = "arn:${data.aws_partition.current.partition}:kms:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:key/*"
      }
    ]
  })
}

resource "aws_iam_role_policy" "gateway_direct_deny" {
  count = var.deny_gateway_direct_access ? 1 : 0
  role  = var.gateway_role_name
  name  = "${var.name_prefix}-context-require-product-session"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Deny", Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"], Resource = "${local.bucket_arn}/tenants/*/products/*" },
      {
        Effect = "Deny", Action = ["s3:ListBucket", "s3:ListBucketVersions"], Resource = local.bucket_arn
        # Listing a parent prefix also reveals product names; all bucket listing
        # must use a scoped session after cutover.
      }
    ]
  })
}

resource "aws_s3_bucket_cors_configuration" "uploads" {
  count  = length(var.upload_origins) > 0 ? 1 : 0
  bucket = var.bucket_name
  cors_rule {
    allowed_methods = ["PUT", "GET", "HEAD"]
    allowed_origins = var.upload_origins
    allowed_headers = ["content-type", "content-length", "x-amz-checksum-sha256", "x-amz-server-side-encryption", "x-amz-server-side-encryption-aws-kms-key-id", "x-amz-server-side-encryption-bucket-key-enabled"]
    expose_headers  = ["ETag", "x-amz-version-id"]
    max_age_seconds = 300
  }
}
