# =============================================================================
# KMS Key for DynamoDB Table Encryption (CKV_AWS_119)
# =============================================================================
# Single customer-managed KMS key for all DynamoDB tables in this module.
# Satisfies Checkov CKV_AWS_119 compliance requirement.
# =============================================================================

resource "aws_kms_key" "dynamodb" {
  description             = "Customer-managed KMS key for Gateway DynamoDB tables"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = data.aws_iam_policy_document.dynamodb_kms.json

  tags = merge(local.common_tags, {
    Name    = "adp-${var.environment}-gateway-dynamodb"
    Service = "kms"
    Purpose = "dynamodb-encryption"
  })
}

resource "aws_kms_alias" "dynamodb" {
  name          = "alias/adp-${var.environment}-gateway-dynamodb"
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
# Single customer-managed KMS key for all CloudWatch Log Groups in this module.
# =============================================================================

resource "aws_kms_key" "cloudwatch" {
  description             = "Customer-managed KMS key for Gateway CloudWatch Log Groups"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = data.aws_iam_policy_document.cloudwatch_kms.json

  tags = merge(local.common_tags, {
    Name    = "adp-${var.environment}-gateway-cloudwatch"
    Service = "kms"
    Purpose = "cloudwatch-log-encryption"
  })
}

resource "aws_kms_alias" "cloudwatch" {
  name          = "alias/adp-${var.environment}-gateway-cloudwatch"
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
# KMS Key for Secrets Manager Encryption (CKV_AWS_149)
# =============================================================================

resource "aws_kms_key" "secrets" {
  description             = "Customer-managed KMS key for Gateway Secrets Manager secrets"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = data.aws_iam_policy_document.secrets_kms.json

  tags = merge(local.common_tags, {
    Name    = "adp-${var.environment}-gateway-secrets"
    Service = "kms"
    Purpose = "secrets-encryption"
  })
}

resource "aws_kms_alias" "secrets" {
  name          = "alias/adp-${var.environment}-gateway-secrets"
  target_key_id = aws_kms_key.secrets.id
}

data "aws_iam_policy_document" "secrets_kms" {
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
    sid    = "SecretsManagerService"
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
      identifiers = ["secretsmanager.amazonaws.com"]
    }
  }
}
