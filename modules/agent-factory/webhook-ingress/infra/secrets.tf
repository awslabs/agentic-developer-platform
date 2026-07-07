# =============================================================================
# Secrets Manager — Webhook Secret
# =============================================================================
# Placeholder for the GitHub webhook secret used for HMAC validation.
# The actual secret value is set out-of-band (during GitHub App setup).
# =============================================================================

resource "aws_secretsmanager_secret" "webhook_secret" {
  name        = "adp/${var.environment}/webhook-ingress/github-webhook-secret"
  description = "GitHub webhook secret for HMAC-SHA256 signature validation"
  kms_key_id  = aws_kms_key.secrets.arn
}

# Placeholder value — will be overwritten during GitHub App webhook configuration
resource "aws_secretsmanager_secret_version" "webhook_secret" {
  secret_id     = aws_secretsmanager_secret.webhook_secret.id
  secret_string = "PLACEHOLDER_REPLACE_WITH_ACTUAL_SECRET"

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# =============================================================================
# Secrets Manager — ADP Agent Platform GitHub App
# =============================================================================
# The public GitHub App that customers install. Credentials are set by the
# register-github-app.sh script after browser-based app creation.
# =============================================================================

resource "aws_secretsmanager_secret" "github_app_id" {
  name        = "adp/${var.environment}/github-app/adp-agent-platform-id"
  description = "GitHub App ID for the ADP Agent Platform public app"
  kms_key_id  = aws_kms_key.secrets.arn
}

resource "aws_secretsmanager_secret_version" "github_app_id" {
  secret_id     = aws_secretsmanager_secret.github_app_id.id
  secret_string = "PLACEHOLDER_SET_BY_REGISTER_SCRIPT"

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret" "github_app_key" {
  name        = "adp/${var.environment}/github-app/adp-agent-platform-key"
  description = "Private key (PEM) for the ADP Agent Platform public app"
  kms_key_id  = aws_kms_key.secrets.arn
}

resource "aws_secretsmanager_secret_version" "github_app_key" {
  secret_id     = aws_secretsmanager_secret.github_app_key.id
  secret_string = "PLACEHOLDER_SET_BY_REGISTER_SCRIPT"

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# =============================================================================
# Secrets Manager — Correlation Marker Signing Key (Issue #3178)
# =============================================================================
# HMAC-SHA256 key used by the agent worker to sign correlation markers.
# Prevents marker forgery (cred-binding S4). Verification is in S5.
# The actual key value is generated out-of-band (e.g. `openssl rand -base64 32`)
# and stored via CLI or rotation Lambda. 90-day rotation; previous version
# retained 7 days for graceful rollover during S5 verification.
# =============================================================================

resource "aws_secretsmanager_secret" "marker_signing_key" {
  name        = "adp/${var.environment}/webhook-ingress/marker-signing-key"
  description = "HMAC-SHA256 key for signing correlation markers (cred-binding S4)"
  kms_key_id  = aws_kms_key.secrets.arn

  tags = {
    Purpose  = "marker-signing"
    Rotation = "90-day"
  }
}

resource "aws_secretsmanager_secret_version" "marker_signing_key" {
  secret_id     = aws_secretsmanager_secret.marker_signing_key.id
  secret_string = "PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND"

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# Note: 90-day rotation with previous-version retention (7 days) will be
# configured when the rotation Lambda is provisioned. Until then, manual
# rotation via: aws secretsmanager put-secret-value --secret-id <arn> \
#   --secret-string "$(openssl rand -base64 32)"
