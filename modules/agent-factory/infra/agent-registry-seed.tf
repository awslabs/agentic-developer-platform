# =============================================================================
# Agent Registry Seed — register worker IRSA roles in the gateway's DynamoDB
# =============================================================================
# Issue #575: Worker pods authenticate to /internal/v1/* via IRSA/SigV4.
# The gateway's TokenContextMiddleware looks up the caller's IAM role ARN in
# the agent_registry DynamoDB table. This resource seeds the entry for the
# scaledjob worker role (shared by chat + webhook workers).
#
# One entry per IRSA role — all workers using adp-<env>-agent-scaledjob-role
# (chat, webhook, future) share this single entry.
# =============================================================================

variable "agent_registry_table_name" {
  description = "DynamoDB agent registry table name (from gateway infra output). Empty disables seeding."
  type        = string
  default     = ""
}

resource "aws_dynamodb_table_item" "scaledjob_worker_agent" {
  count = var.agent_registry_table_name != "" ? 1 : 0

  table_name = var.agent_registry_table_name
  hash_key   = "agent_id"

  item = jsonencode({
    agent_id         = { S = "scaledjob-worker" }
    role_arn         = { S = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/adp-${var.environment}-agent-scaledjob-role" }
    agent_name       = { S = "scaledjob-worker" }
    org_id           = { S = "__platform__" }
    team_id          = { S = "__agents__" }
    owner            = { S = "platform" }
    scope            = { S = "internal" }
    budget_config_id = { S = "" }
    allowed_models   = { SS = ["*"] }
    status           = { S = "active" }
    description      = { S = "Hosted agent worker pods (webhook + chat ScaledJob). Authenticates via IRSA to /internal/v1/* endpoints." }
    image_uri        = { S = "" }
    code_repo        = { S = "" }
    workflow_name    = { S = "" }
    created_at       = { S = "2026-05-12T00:00:00Z" }
    updated_at       = { S = "2026-05-12T00:00:00Z" }
  })

  lifecycle {
    ignore_changes = [item]
  }
}
