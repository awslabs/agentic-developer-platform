# =============================================================================
# Adversarial E2E Test Infrastructure — Issue #3377
# =============================================================================
# Resources consumed by the credential-binding adversarial E2E workflow
# (.github/workflows/credential-binding-adversarial-e2e.yml, S8 of EPIC #3172).
#
# Two gaps blocked the workflow from running:
#   1. SSM SecureString /adp/<env>/gateway/internal-api-key — the workflow reads
#      the gateway internal API key from SSM (with-decryption), but the key only
#      existed in Secrets Manager (created by gateway-deploy.yml). This resource
#      mirrors it into SSM.
#   2. S3 bucket + SSM param /adp/<env>/adversarial-tests/evidence-bucket — the
#      workflow uploads evidence (transcripts, assertion reports) to S3 for
#      durable archival and audit.
#
# Gated by var.enable_adversarial_e2e (default false) — the Secrets Manager
# secret must exist before these resources can plan. Fresh deploy-all.sh runs
# (where CI has not seeded the secret) leave this disabled. Issue #3488.
# =============================================================================

# -----------------------------------------------------------------------------
# 1. Internal API Key — Mirror from Secrets Manager to SSM SecureString
# -----------------------------------------------------------------------------
# The gateway deploy workflow stores the internal API key in Secrets Manager at
# "adp/<env>/gateway/internal-api-key". The adversarial E2E workflow reads it
# from SSM Parameter Store at "/adp/<env>/gateway/internal-api-key" (with
# --with-decryption). This data source + SSM resource bridges the two.
#
# ignore_changes on `value` ensures Terraform doesn't clobber the key on every
# apply — the value is seeded once and may be rotated out-of-band.

data "aws_secretsmanager_secret" "gateway_internal_api_key" {
  count = var.enable_adversarial_e2e ? 1 : 0
  name  = "adp/${var.environment}/gateway/internal-api-key"
}

data "aws_secretsmanager_secret_version" "gateway_internal_api_key" {
  count     = var.enable_adversarial_e2e ? 1 : 0
  secret_id = data.aws_secretsmanager_secret.gateway_internal_api_key[0].id
}

resource "aws_ssm_parameter" "gateway_internal_api_key" {
  count       = var.enable_adversarial_e2e ? 1 : 0
  name        = "/adp/${var.environment}/gateway/internal-api-key"
  description = "Gateway internal API key for /internal/v1/* shared-secret auth (mirrored from Secrets Manager for E2E workflow consumption)"
  type        = "SecureString"
  value       = data.aws_secretsmanager_secret_version.gateway_internal_api_key[0].secret_string

  tags = {
    Purpose   = "adversarial-e2e"
    Source    = "secrets-manager-mirror"
    Issue     = "3377"
    Component = "credential-binding"
  }

  lifecycle {
    ignore_changes = [value]
  }
}

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
