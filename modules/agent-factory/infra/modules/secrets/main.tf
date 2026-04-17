# =============================================================================
# Secrets Manager — GitHub App credentials for agent personas
# =============================================================================
# The secrets themselves are created by platform/scripts/create-github-apps.sh
# during the interactive Phase 0 setup (the script also stores the App IDs and
# private keys). This Terraform file only *references* them so the module can
# expose a stable prefix to downstream modules.
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
