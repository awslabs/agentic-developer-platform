# Tools consent posture (#8387).
#
# Rendered by both gateway renderers (.github/workflows/gateway-deploy.yml and
# platform/scripts/deploy-all.sh) into the ConfigMap's ADP_TOOLS_CONSENT_DEFAULT.
#   granted  - a human owner is allowed and consented to every tool the tenant
#              entitles for their team, minus the tools they opt out of; an
#              administrator still decides entitlement, ceilings and connections.
#   explicit - the pre-#8387 opt-in contract: an admin per-user allow list and the
#              owner ticking each tool in Settings -> Tools.
# A tenant overrides the platform value in Organization.settings.tools.consent_default
# (admin Tool access page). Invalid values fail closed to explicit in the gateway.
variable "tools_consent_default" {
  type        = string
  description = "Platform-wide tools consent posture rendered as ADP_TOOLS_CONSENT_DEFAULT: granted (default) or explicit."
  default     = "granted"

  validation {
    condition     = contains(["granted", "explicit"], var.tools_consent_default)
    error_message = "tools_consent_default must be granted or explicit."
  }
}

resource "aws_ssm_parameter" "tools_consent_default" {
  name  = "/adp/${var.environment}/gateway/tools-consent-default"
  type  = "String"
  value = var.tools_consent_default
}
