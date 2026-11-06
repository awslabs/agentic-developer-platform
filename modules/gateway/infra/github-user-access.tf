# GitHub as the owner for the owner's own tools (#8555).
#
# Rendered by both gateway renderers (.github/workflows/gateway-deploy.yml and
# platform/scripts/deploy-all.sh) into the ConfigMap's ADP_GITHUB_USER_ACCESS_ENABLED.
# While off, the Dev Box host route POST /agent/crew/github-token and the owner's
# POST /auth/connections/oauth/github/token answer 503 github_user_access_disabled
# (mount-always, 503-when-disabled); the GitHub connection itself and the GitHub
# tool are unaffected. Default off: turning it on is an environment decision.
variable "github_user_access_enabled" {
  type        = bool
  description = "Let the owner's Dev Box (gh/git) and `adp github token` act as the owner through their connected GitHub account."
  default     = false
}

resource "aws_ssm_parameter" "github_user_access_enabled" {
  name  = "/adp/${var.environment}/gateway/github-user-access-enabled"
  type  = "String"
  value = var.github_user_access_enabled ? "true" : "false"
}
