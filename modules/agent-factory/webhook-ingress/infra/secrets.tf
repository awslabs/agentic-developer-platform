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

# =============================================================================
# Secrets Manager — ADP Agent Platform GitHub App
# =============================================================================
# The public GitHub App that customers install. Credentials are set by the
# register-github-app.sh script after browser-based app creation.
# =============================================================================

resource "aws_secretsmanager_secret" "github_app_id" {
  name        = "adp/${var.environment}/github-app/adp-agent-platform-id"
  description = "GitHub App ID for the ADP Agent Platform public app"
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
}

resource "aws_secretsmanager_secret_version" "github_app_key" {
  secret_id     = aws_secretsmanager_secret.github_app_key.id
  secret_string = "PLACEHOLDER_SET_BY_REGISTER_SCRIPT"

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# =============================================================================
# K8s Secret — vault-internal-api-key (consumed by KEDA-spawned agent pods)
# =============================================================================
# Shared secret between the gateway pod (validates inbound /internal/v1/* calls)
# and the KEDA-spawned agent pods (sources it as VAULT_INTERNAL_API_KEY env).
# Without this secret, the ScaledJob spec is invalid and agent pods don't spawn —
# the failure mode flagged on deploy-instance #1062.
#
# The Secrets Manager value is created + populated by gateway-deploy.yml during
# Phase 5 (alongside token-secret-key), so by the time this Phase 7 terraform
# runs the value already exists. We read it via a data source so the K8s Secret
# rotates automatically when the operator rotates the gateway-side secret.
data "aws_secretsmanager_secret" "internal_api_key" {
  name = "adp/${var.environment}/gateway/internal-api-key"
}

data "aws_secretsmanager_secret_version" "internal_api_key" {
  secret_id = data.aws_secretsmanager_secret.internal_api_key.id
}

resource "kubernetes_secret" "vault_internal_api_key" {
  metadata {
    name      = "vault-internal-api-key"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name

    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
    }
  }

  type = "Opaque"

  data = {
    VAULT_INTERNAL_API_KEY = data.aws_secretsmanager_secret_version.internal_api_key.secret_string
  }
}
