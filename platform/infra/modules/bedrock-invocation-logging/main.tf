terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

locals {
  account_id     = data.aws_caller_identity.current.account_id
  region         = data.aws_region.current.name
  partition      = data.aws_partition.current.partition
  dns_suffix     = data.aws_partition.current.dns_suffix
  bedrock_source = "arn:${local.partition}:bedrock:${local.region}:${local.account_id}:*"
  log_group_name = "/aws/bedrock/${var.name_prefix}/model-invocations"
  log_group_arn  = "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:${local.log_group_name}"
  log_prefix     = "invocations"
  large_prefix   = "large-data"
  delivery_path  = "AWSLogs/${local.account_id}/BedrockModelInvocationLogs/*"
  source_condition = {
    StringEquals = { "aws:SourceAccount" = local.account_id }
    ArnLike      = { "aws:SourceArn" = local.bedrock_source }
  }
  tags = merge(var.common_tags, { Purpose = "bedrock-invocation-logs" })
}

# A dedicated key lets log readers be authorized independently of application
# data. The service permissions follow AWS's invocation-logging setup guide.
resource "aws_kms_key" "logs" {
  description             = "${var.name_prefix} Bedrock invocation logs"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = local.tags

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:${local.partition}:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "BedrockS3Delivery"
        Effect    = "Allow"
        Principal = { Service = "bedrock.${local.dns_suffix}" }
        Action    = "kms:GenerateDataKey"
        Resource  = "*"
        Condition = local.source_condition
      },
      {
        Sid       = "CloudWatchLogEncryption"
        Effect    = "Allow"
        Principal = { Service = "logs.${local.region}.${local.dns_suffix}" }
        Action    = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"]
        Resource  = "*"
        Condition = {
          ArnEquals = { "kms:EncryptionContext:aws:logs:arn" = local.log_group_arn }
        }
      },
    ]
  })
}

resource "aws_kms_alias" "logs" {
  name          = "alias/${var.name_prefix}-bedrock-invocation-logs"
  target_key_id = aws_kms_key.logs.key_id
}

resource "aws_cloudwatch_log_group" "invocations" {
  name              = local.log_group_name
  retention_in_days = var.retention_in_days
  kms_key_id        = aws_kms_key.logs.arn
  tags              = local.tags
}

resource "aws_s3_bucket" "logs" {
  # Account and region make a fresh deployment portable across AWS accounts
  # and let separately managed regions use their required local destination.
  bucket        = "${var.name_prefix}-bedrock-logs-${local.account_id}-${local.region}"
  force_destroy = false
  tags          = local.tags
}

resource "aws_s3_bucket_ownership_controls" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.logs.arn
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    id     = "expire-invocation-logs"
    status = "Enabled"
    filter {}
    expiration {
      days = var.retention_in_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_s3_bucket_policy" "logs" {
  bucket = aws_s3_bucket.logs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "BedrockLogDelivery"
        Effect    = "Allow"
        Principal = { Service = "bedrock.${local.dns_suffix}" }
        Action    = "s3:PutObject"
        Resource = [
          "${aws_s3_bucket.logs.arn}/${local.log_prefix}/${local.delivery_path}",
          "${aws_s3_bucket.logs.arn}/${local.large_prefix}/${local.delivery_path}",
        ]
        Condition = local.source_condition
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.logs.arn, "${aws_s3_bucket.logs.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.logs, aws_s3_bucket_ownership_controls.logs]
}

resource "aws_iam_role" "delivery" {
  name = "${var.name_prefix}-bedrock-logging-${local.region}"
  tags = local.tags
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock.${local.dns_suffix}" }
      Action    = "sts:AssumeRole"
      Condition = local.source_condition
    }]
  })
}

resource "aws_iam_role_policy" "delivery" {
  name = "bedrock-invocation-log-delivery"
  role = aws_iam_role.delivery.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "${local.log_group_arn}:log-stream:aws/bedrock/modelinvocations"
    }]
  })
}

resource "aws_bedrock_model_invocation_logging_configuration" "this" {
  count = var.enabled ? 1 : 0

  logging_config {
    text_data_delivery_enabled      = true
    image_data_delivery_enabled     = true
    embedding_data_delivery_enabled = true
    video_data_delivery_enabled     = true

    s3_config {
      bucket_name = aws_s3_bucket.logs.id
      key_prefix  = local.log_prefix
    }

    cloudwatch_config {
      log_group_name = aws_cloudwatch_log_group.invocations.name
      role_arn       = aws_iam_role.delivery.arn

      # Responses/agent prompts frequently exceed CloudWatch's inline 100 KB
      # payload limit. Keep the referenced bodies, not only their metadata.
      large_data_delivery_s3_config {
        bucket_name = aws_s3_bucket.logs.id
        key_prefix  = local.large_prefix
      }
    }
  }

  depends_on = [
    aws_iam_role_policy.delivery,
    aws_s3_bucket_policy.logs,
    aws_s3_bucket_server_side_encryption_configuration.logs,
    aws_s3_bucket_lifecycle_configuration.logs,
  ]
}
