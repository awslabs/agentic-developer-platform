# =============================================================================
# Secrets Manager — GitHub App credentials for agent personas (ARC runner path)
# =============================================================================
# These secrets are created manually during ARC runner setup (see
# modules/agent-factory/SETUP-GUIDE.md). This Terraform file *references* them
# so the module can expose a stable prefix to downstream modules.
#
# NOTE: The primary webhook agent path uses a single App registered via
# register-github-app.sh or the Connections UI, stored at a different path
# (adp/<env>/github-app/adp-agent-platform-*). This module is for the
# complementary ARC self-hosted runner execution model only.
#
# Secret layout (per persona):
#   adp/<github-org>/gh-app-<role>-id     -> App ID (plaintext string)
#   adp/<github-org>/gh-app-<role>-key    -> PEM private key
# where <role> is one of: dev, pm, ops.
# =============================================================================

locals {
  roles          = ["dev", "pm", "ops"]
  secrets_prefix = "adp/${var.github_org}/gh-app-"
}

data "aws_secretsmanager_secret" "app_id" {
  for_each = toset(local.roles)
  name     = "${local.secrets_prefix}${each.key}-id"
}

data "aws_secretsmanager_secret" "app_key" {
  for_each = toset(local.roles)
  name     = "${local.secrets_prefix}${each.key}-key"
}
