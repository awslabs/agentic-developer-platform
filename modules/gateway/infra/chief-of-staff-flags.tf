# Chief-of-Staff surfaces (#7197): chat, the assistant UI and the Crew dashboard are
# enabled per environment through these parameters. gateway-deploy.yml and
# platform/scripts/deploy-all.sh render them into the gateway Deployment
# (FEATURE_CHAT_ENABLED, FEATURE_ASSISTANT_UI_ENABLED, FEATURE_CREW_DASHBOARD,
# CREW_PROXY_ENABLED); absent => off. Declared here so a new environment gets them
# from its tfvars instead of an operator's shell history. The Crew flags follow
# crew_dashboard_enabled, which also creates the dedicated edge and the
# crew-dashboard-origin parameter (crew-dashboard.tf).

variable "feature_chat_enabled" {
  description = "Expose the chat surface (FEATURE_CHAT_ENABLED) in this environment."
  type        = bool
  default     = false
}

variable "feature_assistant_ui_enabled" {
  description = "Expose the assistant installation UI (FEATURE_ASSISTANT_UI_ENABLED); requires chat."
  type        = bool
  default     = false
}

variable "chat_authority_worker_images" {
  description = "Pinned digests (sha256:...) of chat worker images the gateway admits; empty refuses unregistered callers."
  type        = list(string)
  default     = []
  validation {
    condition     = alltrue([for item in var.chat_authority_worker_images : can(regex("^sha256:[0-9a-f]{64}$", item))])
    error_message = "chat_authority_worker_images must contain pinned sha256 digests."
  }
}

resource "aws_ssm_parameter" "feature_chat" {
  name  = "/adp/${var.environment}/gateway/feature-chat"
  type  = "String"
  value = tostring(var.feature_chat_enabled)
}

resource "aws_ssm_parameter" "feature_assistant_ui" {
  name  = "/adp/${var.environment}/gateway/feature-assistant-ui"
  type  = "String"
  value = tostring(var.feature_assistant_ui_enabled)
}

resource "aws_ssm_parameter" "feature_crew_dashboard" {
  name  = "/adp/${var.environment}/gateway/feature-crew-dashboard"
  type  = "String"
  value = tostring(var.crew_dashboard_enabled)
}

resource "aws_ssm_parameter" "crew_proxy_enabled" {
  name  = "/adp/${var.environment}/gateway/crew-proxy-enabled"
  type  = "String"
  value = tostring(var.crew_dashboard_enabled)
}

# Read with --with-decryption by the renderers (get_authority_ssm); a String is fine
# to read that way, and the digests are not secret.
#
# The deploy publishes release digests here: `deploy-all.sh --chat delegated`
# (Step 10c, #7899) overwrites the value with the supervisor image digest after
# resolving the build, so Terraform only seeds the parameter and never reverts
# the published value (lifecycle ignore_changes). The tfvars value is the seed
# for a fresh environment.
resource "aws_ssm_parameter" "chat_authority_worker_images" {
  name  = "/adp/${var.environment}/gateway/chat-authority-worker-images"
  type  = "String"
  value = length(var.chat_authority_worker_images) > 0 ? join(",", var.chat_authority_worker_images) : " "
  lifecycle {
    ignore_changes = [value]
  }
}

# Delegated chat workload identities (#7899). The gateway verifies chat pods by
# image digest per service account (src/agentauth/workload.py): the supervisor
# through ADP_CHAT_WORKER_IMAGE_DIGESTS + ADP_CHAT_WORKER_SERVICE_ACCOUNT, sandbox
# pods (adp-chat-sandbox) through ADP_CHAT_SANDBOX_IMAGE_DIGESTS and Crew pods
# (adp-crew-pod) through ADP_CHAT_CREW_IMAGE_DIGESTS. Terraform seeds the three
# parameters empty / legacy; `deploy-all.sh --chat delegated` publishes the
# release digests and switches the worker service account to adp-chat-supervisor,
# so the values are script-owned (ignore_changes). " " is SSM's empty value.
resource "aws_ssm_parameter" "chat_sandbox_image_digests" {
  name  = "/adp/${var.environment}/gateway/chat-sandbox-image-digests"
  type  = "String"
  value = " "
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "chat_crew_image_digests" {
  name  = "/adp/${var.environment}/gateway/chat-crew-image-digests"
  type  = "String"
  value = " "
  lifecycle {
    ignore_changes = [value]
  }
}

# The gateway's chat data plane (/internal/v1/agent/chat/data/*, which the
# delegated supervisor and sandboxes call) answers 503 chat_data_disabled unless
# ADP_CHAT_DATA_ENABLED=true (src/agentauth/chat_data_routes.py). Terraform seeds
# it off; `deploy-all.sh --chat delegated` publishes true, so the value is
# script-owned like the identities above.
resource "aws_ssm_parameter" "chat_data_enabled" {
  name  = "/adp/${var.environment}/gateway/chat-data-enabled"
  type  = "String"
  value = "false"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "chat_worker_service_account" {
  name  = "/adp/${var.environment}/gateway/chat-worker-service-account"
  type  = "String"
  value = "adp-agent"
  lifecycle {
    ignore_changes = [value]
  }
}

# Deployment default for the executor a persistent chat session's pod runs when the session
# has not chosen one (#8115): sandbox-loop (minimal look-up loop) | claude-code (Claude Agent
# SDK in the sandbox, #7208) | kirocrew. deploy-chat-scaledjob.sh renders it into the chat
# ScaledJob as ADP_CHAT_PERSISTENT_EXECUTOR; a session's own choice (PUT
# /chat/sessions/{id}/mode executor) outranks it. Seeded to the minimal loop and
# script-owned like the values above: `deploy-all.sh --chat delegated` publishes the
# operator's ADP_CHAT_PERSISTENT_EXECUTOR (dev: claude-code).
resource "aws_ssm_parameter" "chat_persistent_executor" {
  name  = "/adp/${var.environment}/gateway/chat-persistent-executor"
  type  = "String"
  value = "sandbox-loop"
  lifecycle {
    ignore_changes = [value]
  }
}

# Model-root bindings (#5425, #7899). With ADP_CHAT_MODEL_POLICY_ENABLED the ingest
# Lambda registers every chat turn as a human-rooted model root through
# POST /internal/v1/agent/roots/admit; the gateway admits the call only when a
# binding names the producer role, the separate chat supervisor identity, the
# tenant and the personas it may root (src/agentauth/external_roots.py). The
# renderers read /adp/<env>/gateway/model-root-bindings into
# ADP_MODEL_ROOT_BINDINGS. An empty list refuses every producer, which is the
# safe default for an environment that has not enabled delegated chat. Role ARNs
# are account-specific, so a fresh environment sets them in its tfvars (the
# ingest role is agent-factory output `ingest_role_arn`; the supervisor role is
# `chat_supervisor_role_arn`).
variable "model_root_bindings" {
  description = "Producer bindings the gateway admits model roots for (source, producer_role, chat_supervisor_role, tenant_id, personas, optional instance/project_id/repo)."
  type = list(object({
    source               = string
    producer_role        = string
    chat_supervisor_role = optional(string)
    tenant_id            = string
    personas             = list(string)
    instance             = optional(string, "")
    project_id           = optional(number, 0)
    repo                 = optional(string, "")
  }))
  default = []
  validation {
    condition = alltrue([
      for b in var.model_root_bindings :
      contains(["chat", "gitlab"], b.source)
      && can(regex("^arn:aws(-us-gov|-cn)?:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$", b.producer_role))
      && length(b.tenant_id) > 0 && length(b.personas) > 0
      && (b.source != "chat" || (b.chat_supervisor_role != null && b.chat_supervisor_role != b.producer_role))
    ])
    error_message = "Each binding needs a chat|gitlab source, an IAM role ARN producer, a tenant, personas, and for chat a distinct chat_supervisor_role."
  }
}

resource "aws_ssm_parameter" "model_root_bindings" {
  name = "/adp/${var.environment}/gateway/model-root-bindings"
  type = "String"
  value = jsonencode([
    for b in var.model_root_bindings : merge(
      {
        source        = b.source
        producer_role = b.producer_role
        tenant_id     = b.tenant_id
        personas      = b.personas
        instance      = b.instance
        project_id    = b.project_id
        repo          = b.repo
      },
      b.chat_supervisor_role == null ? {} : { chat_supervisor_role = b.chat_supervisor_role }
    )
  ])
}

# Crew proxy tuning (#7902). Rendered by the same renderers into
# CREW_PROXY_KEEPALIVE_SECONDS (SSE comment / pod-hop ping interval; the browser
# hop is uvicorn's protocol ping, UVICORN_WS_PING_INTERVAL, from the same value
# — omit it when 0) and CREW_PROXY_MAX_CONN_PER_USER (src/crew/connections.py
# caps it at 64; the Kiro Crew SPA fetches ~28 chunks concurrently on first load). Keep the keepalive below CloudFront's 60-second VPC-origin read
# timeout when crew_dashboard_edge = "cloudfront".
variable "crew_proxy_keepalive_seconds" {
  description = "Seconds of silence before the Crew proxy emits an SSE keepalive comment or pings the pod; 0 disables."
  type        = number
  default     = 25
  validation {
    condition     = var.crew_proxy_keepalive_seconds >= 0 && floor(var.crew_proxy_keepalive_seconds) == var.crew_proxy_keepalive_seconds
    error_message = "crew_proxy_keepalive_seconds must be a whole number of seconds, 0 or more."
  }
}

variable "crew_proxy_max_conn_per_user" {
  description = "Concurrent Crew proxy connections admitted per user (CREW_PROXY_MAX_CONN_PER_USER); the gateway caps this at 64."
  type        = number
  default     = 64
  validation {
    condition     = var.crew_proxy_max_conn_per_user >= 1 && var.crew_proxy_max_conn_per_user <= 64
    error_message = "crew_proxy_max_conn_per_user must be between 1 and 64."
  }
}

resource "aws_ssm_parameter" "crew_proxy_keepalive_seconds" {
  name  = "/adp/${var.environment}/gateway/crew-proxy-keepalive-seconds"
  type  = "String"
  value = tostring(var.crew_proxy_keepalive_seconds)
}

resource "aws_ssm_parameter" "crew_proxy_max_conn_per_user" {
  name  = "/adp/${var.environment}/gateway/crew-proxy-max-conn-per-user"
  type  = "String"
  value = tostring(var.crew_proxy_max_conn_per_user)
}

# Model policy posture for chat (#7997). Rendered by both renderers into the gateway
# ConfigMap as BG_CHAT_MODEL_POLICY_POSTURE (src/shared/config.py). Governs how
# POST /v1/chat/model/decision treats the live model-policy decision for a
# delegated chat turn:
#   enforced - a verified, enforcing decision backed by fresh invocability evidence
#              is required; without it the turn is refused (503 probing_disabled).
#              Needs model probing armed (model-probe.tf + webhook-ingress CronJob).
#   advisory - default. The decision is still produced, signed and journaled, but
#              rejected or unverified evidence no longer refuses the turn: the
#              persona's resolved model is returned marked advisory with the reason.
#   off      - no invocability evidence is consulted.
# Tenant/persona allowlist refusals (not_permitted) refuse in every posture, and
# autonomous agent runs are unaffected (they keep the registry's runtime posture).
variable "chat_model_policy_posture" {
  description = "Chat model-policy posture (BG_CHAT_MODEL_POLICY_POSTURE): off, advisory (default) or enforced."
  type        = string
  default     = "advisory"
  validation {
    condition     = contains(["off", "advisory", "enforced"], var.chat_model_policy_posture)
    error_message = "chat_model_policy_posture must be off, advisory or enforced."
  }
}

resource "aws_ssm_parameter" "chat_model_policy_posture" {
  name  = "/adp/${var.environment}/gateway/chat-model-policy-posture"
  type  = "String"
  value = var.chat_model_policy_posture
}
