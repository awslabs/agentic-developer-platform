# =============================================================================
# KMS Key for DynamoDB Table Encryption (CKV_AWS_119)
# =============================================================================
# Single customer-managed KMS key for all DynamoDB tables in this module.
# Covers: tenant_registry, webhook_events, rate_limits.
# =============================================================================

resource "aws_kms_key" "dynamodb" {
  description             = "Customer-managed KMS key for Webhook Ingress DynamoDB tables"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = data.aws_iam_policy_document.dynamodb_kms.json

  tags = {
    Component = "webhook-ingress"
    Service   = "kms"
    Purpose   = "dynamodb-encryption"
  }
}

resource "aws_kms_alias" "dynamodb" {
  name          = "alias/${local.name_prefix}-webhook-dynamodb"
  target_key_id = aws_kms_key.dynamodb.id
}

data "aws_iam_policy_document" "dynamodb_kms" {
  statement {
    sid       = "AccountRoot"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  statement {
    sid    = "DynamoDBService"
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
      "kms:CreateGrant",
    ]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["dynamodb.amazonaws.com"]
    }
  }
}

# =============================================================================
# KMS Key for CloudWatch Log Group Encryption (CKV_AWS_158)
# =============================================================================

resource "aws_kms_key" "cloudwatch" {
  description             = "Customer-managed KMS key for Webhook Ingress CloudWatch Log Groups"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = data.aws_iam_policy_document.cloudwatch_kms.json

  tags = {
    Component = "webhook-ingress"
    Service   = "kms"
    Purpose   = "cloudwatch-log-encryption"
  }
}

resource "aws_kms_alias" "cloudwatch" {
  name          = "alias/${local.name_prefix}-webhook-cloudwatch"
  target_key_id = aws_kms_key.cloudwatch.id
}

data "aws_iam_policy_document" "cloudwatch_kms" {
  statement {
    sid       = "AccountRoot"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  statement {
    sid    = "CloudWatchLogsService"
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["logs.${var.aws_region}.amazonaws.com"]
    }

    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:*"]
    }
  }
}

# =============================================================================
# KMS Key for Secrets Manager Encryption — Data Source (Issue #3789)
# =============================================================================
# The webhook-secrets CMK is now owned by platform infra (alias/adp-<env>-
# webhook-secrets). Platform applies first in all deploy tracks, so the alias
# is guaranteed to exist when this module's plan runs.
#
# Previously: this module created the key + alias (aws_kms_key.secrets +
# aws_kms_alias.secrets). Migrated to platform/infra/kms.tf by #3789.
# Use migrate-webhook-kms.sh for existing environments (state rm + import).
# =============================================================================

data "aws_kms_alias" "secrets" {
  name = "alias/${local.name_prefix}-webhook-secrets"
}

locals {
  # Convenience — all references previously using aws_kms_key.secrets.arn now
  # use this local. The alias data source exposes .target_key_arn which is the
  # key ARN (not the alias ARN).
  webhook_secrets_kms_key_arn = data.aws_kms_alias.secrets.target_key_arn
}
