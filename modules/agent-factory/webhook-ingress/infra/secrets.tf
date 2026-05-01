# =============================================================================
# Secrets Manager — Webhook Secret
# =============================================================================
# Placeholder for the GitHub webhook secret used for HMAC validation.
# The actual secret value is set out-of-band (during GitHub App setup).
# =============================================================================

resource "aws_secretsmanager_secret" "webhook_secret" {
  name        = "adp/${var.environment}/webhook-ingress/github-webhook-secret"
  description = "GitHub webhook secret for HMAC-SHA256 signature validation"
}

# Placeholder value — will be overwritten during GitHub App webhook configuration
resource "aws_secretsmanager_secret_version" "webhook_secret" {
  secret_id     = aws_secretsmanager_secret.webhook_secret.id
  secret_string = "PLACEHOLDER_REPLACE_WITH_ACTUAL_SECRET"

  lifecycle {
    ignore_changes = [secret_string]
  }
}
