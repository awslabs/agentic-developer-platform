# =============================================================================
# S3 Bucket: Agent Run Logs (Transcripts) — Issue #3057
# =============================================================================
# Stores full untruncated Markdown transcripts from each agent run. These
# persist beyond the GitHub Check Run 65,535-char limit and check-run retention
# policies, giving operators a durable, auditable archive of what agents did.
#
# Object key layout:
#   {org}/{repo}/issue-{issue_number}/{utc_timestamp}-{run_id}.md
#
# Lifecycle: objects expire after 180 days. No versioning (transcripts are
# write-once, immutable). SSE-S3 encryption, block-public-access all-on.
# =============================================================================

resource "aws_s3_bucket" "agent_run_logs" {
  bucket = "adp-${var.environment}-agent-run-logs-${local.account_id}"

  tags = {
    Name      = "adp-${var.environment}-agent-run-logs"
    Component = "hosted-agent-worker"
    Purpose   = "agent-run-transcripts"
  }
}

resource "aws_s3_bucket_public_access_block" "agent_run_logs" {
  bucket = aws_s3_bucket.agent_run_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "agent_run_logs" {
  bucket = aws_s3_bucket.agent_run_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "agent_run_logs" {
  bucket = aws_s3_bucket.agent_run_logs.id

  rule {
    id     = "expire-after-180-days"
    status = "Enabled"

    expiration {
      days = 180
    }
  }
}
