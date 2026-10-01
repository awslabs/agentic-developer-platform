# Adversarial test evidence storage. The former broad internal-key SSM mirror
# is retired; inspection callers use explicitly scoped IAM identities.

# -----------------------------------------------------------------------------
# 2. Adversarial Evidence S3 Bucket
# -----------------------------------------------------------------------------
# Stores test evidence from adversarial E2E runs: assertion reports, transcripts,
# audit entries. Private, SSE-encrypted, 90-day lifecycle (evidence is ephemeral
# test output, not compliance data).

resource "aws_s3_bucket" "adversarial_evidence" {
  count  = var.enable_adversarial_e2e ? 1 : 0
  bucket = "adp-${var.environment}-adversarial-evidence-${local.account_id}"

  tags = {
    Name      = "adp-${var.environment}-adversarial-evidence"
    Component = "credential-binding"
    Purpose   = "adversarial-e2e-evidence"
    Issue     = "3377"
  }
}

resource "aws_s3_bucket_public_access_block" "adversarial_evidence" {
  count  = var.enable_adversarial_e2e ? 1 : 0
  bucket = aws_s3_bucket.adversarial_evidence[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "adversarial_evidence" {
  count  = var.enable_adversarial_e2e ? 1 : 0
  bucket = aws_s3_bucket.adversarial_evidence[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "adversarial_evidence" {
  count  = var.enable_adversarial_e2e ? 1 : 0
  bucket = aws_s3_bucket.adversarial_evidence[0].id

  rule {
    id     = "expire-after-90-days"
    status = "Enabled"

    expiration {
      days = 90
    }
  }
}

# -----------------------------------------------------------------------------
# 3. SSM Parameter — Evidence Bucket Name
# -----------------------------------------------------------------------------
# The adversarial E2E workflow resolves the bucket name from this parameter.

resource "aws_ssm_parameter" "adversarial_evidence_bucket" {
  count       = var.enable_adversarial_e2e ? 1 : 0
  name        = "/adp/${var.environment}/adversarial-tests/evidence-bucket"
  description = "S3 bucket name for adversarial E2E test evidence (credential-binding S8)"
  type        = "String"
  value       = aws_s3_bucket.adversarial_evidence[0].id

  tags = {
    Purpose   = "adversarial-e2e"
    Issue     = "3377"
    Component = "credential-binding"
  }
}

# -----------------------------------------------------------------------------
# 4. Sandbox Tenant Config SSM Parameters — Issue #3462
# -----------------------------------------------------------------------------
# Sandbox feature availability may be inspected in SSM, but mandatory run
# authorization is attested by the serving gateway's authenticated_run report.
# Historical enforce-credential-binding parameters are no longer consumed and
# must not be reseeded as evidence of authorization. Their deletion is separate
# operator cleanup; this source change does not mutate existing parameters.
