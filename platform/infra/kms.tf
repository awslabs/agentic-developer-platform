# =============================================================================
# KMS Key for CloudWatch Log Group Encryption (CKV_AWS_158)
# =============================================================================
# Single customer-managed KMS key for all CloudWatch Log Groups in the
# platform module (ECR logs, CodeBuild logs, etc.).
# =============================================================================

resource "aws_kms_key" "cloudwatch" {
  description             = "Customer-managed KMS key for Platform CloudWatch Log Groups"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = data.aws_iam_policy_document.cloudwatch_kms.json

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-platform-cloudwatch"
    Service = "kms"
    Purpose = "cloudwatch-log-encryption"
  })
}

resource "aws_kms_alias" "cloudwatch" {
  name          = "alias/${local.name_prefix}-platform-cloudwatch"
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
