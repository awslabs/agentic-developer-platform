# =============================================================================
# URL Analysis Evidence — S3 bucket for screenshots + evidence envelopes
# =============================================================================
# Issue #499: Persist url-analysis screenshots and Evidence JSON to S3 with
# presigned URLs for inline rendering in GitHub issue comments.
#
# Key layout: tenant=<tenant_id>/issue=<issue_number>/run=<run_id>/url-<n>/...
# Lifecycle: 30-day expiration (bounded steady-state storage).
# Access: resource-based policy grants data-plane actions to both the cyber
# worker role AND the agent-factory scaledjob role.
#
# NOTE on bucket name suffix: the original bucket (no suffix) self-locked
# when its policy used `s3:*` Deny — that Deny caught even bucket-management
# operations (PutBucketPolicy, DeleteBucketPolicy, DeleteBucket), leaving
# the bucket only reachable from the two allowlisted roles. Recovery requires
# account root. To unblock the URL-analysis flow without waiting on root,
# this module now provisions a v2 bucket with a *narrow* Deny (data-plane
# only). The orphaned v1 bucket (`adp-<env>-url-analysis-evidence-<acct>`)
# stays behind until root-level cleanup. See PR #509 thread for context.
# =============================================================================

resource "aws_s3_bucket" "url_analysis_evidence" {
  bucket = "adp-${var.environment}-url-analysis-evidence-v2-${data.aws_caller_identity.current.account_id}"

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
    sid    = "DenyOtherDataPlaneAccess"
    effect = "Deny"
    # Data-plane actions only. We deliberately do NOT include `s3:*` here —
    # a wildcard Deny catches bucket-management operations (GetBucketPolicy,
    # PutBucketPolicy, DeleteBucketPolicy, DeleteBucket, etc.), which locks
    # the bucket out of Terraform / admin recovery paths. The v1 bucket was
    # lost this way. Public-access-block + BPA independently guarantee "no
    # unsolicited public read"; this statement guarantees "only the two
    # allowlisted roles can read or write evidence objects".
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:PutObjectAcl",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:ListBucket",
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
