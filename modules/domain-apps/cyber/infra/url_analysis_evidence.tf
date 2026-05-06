# =============================================================================
# URL Analysis Evidence — S3 bucket for screenshots + evidence envelopes
# =============================================================================
# Issue #499: Persist url-analysis screenshots and Evidence JSON to S3 with
# presigned URLs for inline rendering in GitHub issue comments.
#
# Key layout: tenant=<tenant_id>/issue=<issue_number>/run=<run_id>/url-<n>/...
# Lifecycle: 30-day expiration (bounded steady-state storage).
# Access: resource-based policy grants PutObject/GetObject to both the cyber
# worker role AND the agent-factory scaledjob role.
# =============================================================================

resource "aws_s3_bucket" "url_analysis_evidence" {
  bucket = "adp-${var.environment}-url-analysis-evidence-${data.aws_caller_identity.current.account_id}"

  tags = {
    Component    = "cyber"
    SubComponent = "url-analysis"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "url_analysis_evidence" {
  bucket = aws_s3_bucket.url_analysis_evidence.id

  rule {
    id     = "expire-after-30d"
    status = "Enabled"
    expiration {
      days = 30
    }
  }
}

resource "aws_s3_bucket_public_access_block" "url_analysis_evidence" {
  bucket                  = aws_s3_bucket.url_analysis_evidence.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "url_analysis_evidence" {
  bucket = aws_s3_bucket.url_analysis_evidence.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ---------------------------------------------------------------------------
# Bucket resource policy — grant access to both uploading principals
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "url_analysis_evidence" {
  statement {
    sid    = "AllowEvidenceUploaders"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
    ]
    resources = ["${aws_s3_bucket.url_analysis_evidence.arn}/*"]

    principals {
      type = "AWS"
      identifiers = [
        aws_iam_role.cyber_worker.arn,
        "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/adp-${var.environment}-agent-scaledjob-role",
      ]
    }
  }

  statement {
    sid    = "DenyOthers"
    effect = "Deny"
    actions = [
      "s3:*",
    ]
    resources = [
      aws_s3_bucket.url_analysis_evidence.arn,
      "${aws_s3_bucket.url_analysis_evidence.arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values = [
        aws_iam_role.cyber_worker.arn,
        "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/adp-${var.environment}-agent-scaledjob-role",
      ]
    }
  }
}

resource "aws_s3_bucket_policy" "url_analysis_evidence" {
  bucket = aws_s3_bucket.url_analysis_evidence.id
  policy = data.aws_iam_policy_document.url_analysis_evidence.json
}

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

output "url_analysis_evidence_bucket" {
  value       = aws_s3_bucket.url_analysis_evidence.id
  description = "S3 bucket for url-analysis screenshots and evidence envelopes"
}
