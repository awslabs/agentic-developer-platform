# =============================================================================
# KMS Key for CloudWatch Log Group Encryption (CKV_AWS_158)
# =============================================================================
# Single customer-managed KMS key for all CloudWatch Log Groups in the
# platform module (ECR logs, CodeBuild logs, etc.).
# =============================================================================

# =============================================================================
# KMS Key for Webhook Secrets Encryption (CKV_AWS_149)
# =============================================================================
# Issue #3789: Shared CMK for Secrets Manager secrets consumed by BOTH the
# gateway (reads/writes adp/<env>/github-app/* secrets) and webhook-ingress
# Lambda (decrypts webhook secret, App ID, App key). Ownership lives here in
# platform infra because it is shared infrastructure that must exist BEFORE
# either consumer's Terraform plan runs — platform applies first in all deploy
# tracks (deploy-all.sh, CI pipeline, root deploy.sh).
#
# Previously owned by webhook-ingress (deployed Step 9, after gateway Step 3).
# That ordering gap meant the gateway could never get KMS access on a single-
# pass fresh deploy — see #3789 for the full root-cause analysis.
# =============================================================================

resource "aws_kms_key" "webhook_secrets" {
  description             = "Customer-managed KMS key for webhook-ingress and gateway Secrets Manager secrets"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = data.aws_iam_policy_document.webhook_secrets_kms.json

  tags = merge(local.common_tags, {
    Name      = "${local.name_prefix}-webhook-secrets"
    Component = "shared"
    Service   = "kms"
    Purpose   = "webhook-secrets-encryption"
  })
}

resource "aws_kms_alias" "webhook_secrets" {
  name          = "alias/${local.name_prefix}-webhook-secrets"
  target_key_id = aws_kms_key.webhook_secrets.id
}

data "aws_iam_policy_document" "webhook_secrets_kms" {
  # Account root — delegates to IAM policies for fine-grained grants.
  # This is the same pattern as the existing cloudwatch key above.
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

  # Secrets Manager service — allows the service to use this key for
  # encrypting/decrypting secrets on behalf of IAM principals.
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
