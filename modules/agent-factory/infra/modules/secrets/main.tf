# =============================================================================
# Secrets Manager — GitHub App credentials for agent personas
# =============================================================================
# Each agent persona uses a separate GitHub App for rate limit isolation.
# Secrets are created as empty placeholders; populate via CLI or console.
# =============================================================================

locals {
  # Each agent persona gets its own GitHub App for 5000 req/hr rate limit
  app_secrets = {
    "dev" = "Developer agent GitHub App"
    "pm"  = "PM agent GitHub App"
    "ops" = "Operations/Reviewer agent GitHub App"
  }
}

resource "aws_secretsmanager_secret" "app_id" {
  for_each    = local.app_secrets
  name        = "adp/gh-app-${each.key}-id"
  description = "${each.value} - App ID"

  tags = {
    Component = "agent-factory"
  }
}

resource "aws_secretsmanager_secret" "app_key" {
  for_each    = local.app_secrets
  name        = "adp/gh-app-${each.key}-key"
  description = "${each.value} - Private Key"

  tags = {
    Component = "agent-factory"
  }
}
