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
