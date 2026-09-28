# =============================================================================
# Secrets Manager — Webhook Secret
# =============================================================================
# Empty container for the GitHub webhook secret used for HMAC validation.
# The actual secret value is set out-of-band (during GitHub App setup).
# =============================================================================

resource "aws_secretsmanager_secret" "webhook_secret" {
  name                    = "adp/${var.environment}/webhook-ingress/github-webhook-secret"
  description             = "GitHub webhook secret for HMAC-SHA256 signature validation"
  kms_key_id              = local.webhook_secrets_kms_key_arn
  recovery_window_in_days = 30
}

# Setup/rotation owns values. Preserve existing versions during migration;
# fresh installations remain unconfigured and reject webhook authentication.
removed {
  from = aws_secretsmanager_secret_version.webhook_secret
  lifecycle {
    destroy = false
  }
}

# =============================================================================
# Secrets Manager — ADP Agent Platform GitHub App
# =============================================================================
# The public GitHub App that customers install. Credentials are set by the
# register-github-app.sh script after browser-based app creation.
# =============================================================================

resource "aws_secretsmanager_secret" "github_app_id" {
  name                    = "adp/${var.environment}/github-app/adp-agent-platform-id"
  description             = "GitHub App ID for the ADP Agent Platform public app"
  kms_key_id              = local.webhook_secrets_kms_key_arn
  recovery_window_in_days = 30
}

removed {
  from = aws_secretsmanager_secret_version.github_app_id
  lifecycle {
    destroy = false
  }
}

resource "aws_secretsmanager_secret" "github_app_key" {
  name                    = "adp/${var.environment}/github-app/adp-agent-platform-key"
  description             = "Private key (PEM) for the ADP Agent Platform public app"
  kms_key_id              = local.webhook_secrets_kms_key_arn
  recovery_window_in_days = 30
}

removed {
  from = aws_secretsmanager_secret_version.github_app_key
  lifecycle {
    destroy = false
  }
}

# =============================================================================
# Secrets Manager — Correlation Marker Signing Key (Issue #3178)
# =============================================================================
# HMAC-SHA256 key used by the agent worker to sign correlation markers.
# Prevents marker forgery (cred-binding S4). Verification is in S5.
# The actual key value is generated out-of-band (e.g. `openssl rand -base64 32`)
# and stored by the setup/rotation procedure. Retain the previous version for
# the marker verification grace period.
# =============================================================================

locals { marker_signing_secret_name = "adp/${var.environment}/webhook-ingress/marker-signing-key" }

resource "aws_secretsmanager_secret" "marker_signing_key" {
  name                    = local.marker_signing_secret_name
  description             = "HMAC-SHA256 key for signing correlation markers (cred-binding S4)"
  kms_key_id              = local.webhook_secrets_kms_key_arn
  recovery_window_in_days = 30

  tags = {
    Purpose  = "marker-signing"
    Rotation = "operator-managed"
  }
}

removed {
  from = aws_secretsmanager_secret_version.marker_signing_key
  lifecycle {
    destroy = false
  }
}

# Marker key rotation is operator-managed: publish a generated value with
# put-secret-value, retaining AWSPREVIOUS while existing markers expire.
# No automatic rotation schedule is claimed or installed by this module.

# =============================================================================
# Secrets Manager — GitLab Webhook Secret (Issue #3324)
# =============================================================================
# Shared secret token for X-Gitlab-Token validation. No default value is
# installed: a public placeholder is equivalent to no authentication. Seed a
# high-entropy value out-of-band while configuring Admin → Group → Webhooks.
# =============================================================================

resource "aws_secretsmanager_secret" "gitlab_webhook_secret" {
  count       = var.gitlab_webhook_enabled ? 1 : 0
  name        = "adp/${var.environment}/gitlab-webhook-secret"
  description = "GitLab webhook secret token for X-Gitlab-Token header validation"
  kms_key_id  = local.webhook_secrets_kms_key_arn
}

# Older deployments tracked the initial placeholder version. Secret values are
# now owned by out-of-band setup/rotation; relinquish only Terraform's version
# ownership without removing stages or deleting an existing version on upgrade.
# This does not seed a new value or change the current webhook credential.
removed {
  from = aws_secretsmanager_secret_version.gitlab_webhook_secret

  lifecycle {
    destroy = false
  }
}
