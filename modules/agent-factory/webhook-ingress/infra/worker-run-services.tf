# Source companion to #5513. These resources do not activate worker admission.
# Only activation reads the EXISTING webhook key's AWSCURRENT version; the
# bootstrap placeholder must never become a signing key or be replaced here.
variable "agent_door_service_url" {
  description = "Gateway-only Knowledge Door origin; no path, credentials or query."
  type        = string
  default     = "http://context-mcp.agent-context.svc.cluster.local:5100"
  validation {
    condition     = can(regex("^https?://[a-zA-Z0-9.-]+(:[0-9]+)?$", var.agent_door_service_url))
    error_message = "Knowledge Door must be an HTTP(S) origin without userinfo, path or query."
  }
}

data "aws_secretsmanager_secret_version" "worker_marker" {
  count = var.agent_authority_enabled ? 1 : 0
  # Resolve by its canonical existing name, not a resource scheduled for
  # creation/update: unseeded activation must fail during planning, before any
  # unrelated resource mutation. Preparation creates the placeholder separately.
  secret_id     = local.marker_signing_secret_name
  version_stage = "AWSCURRENT"
}

resource "kubernetes_secret" "worker_run_services" {
  count = var.agent_authority_enabled ? 1 : 0
  metadata {
    name      = "agent-run-services"
    namespace = var.gateway_namespace
  }
  data = {
    marker-signing-key = data.aws_secretsmanager_secret_version.worker_marker[0].secret_string
  }
  lifecycle {
    precondition {
      condition = (
        length(data.aws_secretsmanager_secret_version.worker_marker[0].secret_string) >= 32 &&
        length(data.aws_secretsmanager_secret_version.worker_marker[0].secret_string) <= 8192 &&
        !startswith(upper(trimspace(data.aws_secretsmanager_secret_version.worker_marker[0].secret_string)), "PLACEHOLDER")
      )
      error_message = "Seed the existing webhook marker key before activation; never project an empty, short or placeholder signing key."
    }
  }
}
