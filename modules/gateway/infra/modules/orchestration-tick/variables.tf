# =============================================================================
# Variables for the Orchestration Tick module (Issue #4203)
# =============================================================================

variable "environment" {
  description = "Environment name (e.g., dev, staging, prod)"
  type        = string
}

variable "name_prefix" {
  description = <<-EOT
    Prefix for resource names. NOTE: this module is deliberately NOT passed the
    gateway's `local.name_prefix` (`bedrockgw-<env>`). The tick's function, log
    group and schedule rule names are pinned to `adp-<env>-orchestration-tick`
    by the wave-3 evaluation, so the caller passes `adp-<env>`.
  EOT
  type        = string
}

variable "common_tags" {
  description = "Common tags to apply to all resources"
  type        = map(string)
  default     = {}
}

variable "aws_region" {
  description = "AWS region"
  type        = string
}

# -----------------------------------------------------------------------------
# Container image
# -----------------------------------------------------------------------------

variable "image_uri" {
  description = <<-EOT
    Full ECR image URI (repo:tag) for the gateway image the tick runs from.
    The tick's logic lives in `src/orchestration/tick.py` and needs the async
    SQLAlchemy/asyncpg stack plus the RDS CA bundle, all of which the gateway
    image already contains — see the header of `src/orchestration/tick_handler.py`
    for why this is an image and not a zip.
  EOT
  type        = string
}

# -----------------------------------------------------------------------------
# VPC
# -----------------------------------------------------------------------------

variable "vpc_id" {
  description = "ID of the VPC for Lambda deployment"
  type        = string
}

variable "private_subnet_ids" {
  description = "List of private subnet IDs for Lambda deployment"
  type        = list(string)
}

# -----------------------------------------------------------------------------
# RDS
# -----------------------------------------------------------------------------

variable "rds_security_group_id" {
  description = "Security group ID of the RDS instance (an ingress rule is added to it)"
  type        = string
}

# -----------------------------------------------------------------------------
# VPC interface endpoints
# -----------------------------------------------------------------------------

variable "vpc_endpoint_security_group_id" {
  description = <<-EOT
    Security group ID fronting the shared VPC interface endpoints (an ingress
    rule on 443 is added to it for this Lambda's SG). Empty disables the rule.

    Issue #4316. The tick's egress already allows 443 to 0.0.0.0/0, but the SQS
    interface endpoint has private_dns_enabled=true, so `sqs.<region>.amazonaws.com`
    resolves to the endpoint's private ENIs INSIDE the VPC and there is no public
    fallback path to fall back to. The endpoint's own SG only admits the EKS SGs,
    so the tick's packets are dropped (not refused) and the SendMessage call hangs
    until the Lambda timeout kills the whole invocation — after the dispatch pass
    has already committed `ready -> running`. The result is a durable state change,
    every dispatch counter reading 0, no `tick_report` line emitted at all, and no
    alarm; #4211's stall detection dies with the same invocation.

    Both directions are required: egress on the tick SG (already present, above)
    and ingress on the endpoint SG (this rule).
  EOT
  type        = string
  default     = ""
}

variable "db_host" {
  description = "RDS database hostname"
  type        = string
}

variable "db_port" {
  description = "RDS database port"
  type        = number
  default     = 5432
}

variable "db_name" {
  description = "Database name"
  type        = string
}

variable "db_username" {
  description = "Database username used for IAM auth"
  type        = string
}

variable "rds_resource_id" {
  description = "RDS DbiResourceId, used to scope rds-db:connect to a single dbuser"
  type        = string
  default     = ""
}

# -----------------------------------------------------------------------------
# Schedule and sizing
# -----------------------------------------------------------------------------

variable "tick_schedule" {
  description = <<-EOT
    EventBridge schedule for the tick. Deliberately conservative: the tick is
    DB-bound and every write is idempotent, so a slower cadence costs only
    latency-to-ready, while a fast one multiplies RDS connections for no gain.
  EOT
  type        = string
  default     = "rate(5 minutes)"
}

variable "tick_timeout" {
  description = "Lambda timeout in seconds. Bounded reads mean a tick should finish well inside this."
  type        = number
  default     = 120
}

variable "tick_memory" {
  description = "Lambda memory in MB"
  type        = number
  default     = 512
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days"
  type        = number
  default     = 30
}

variable "cloudwatch_kms_key_arn" {
  description = "Optional KMS key ARN for CloudWatch log encryption"
  type        = string
  default     = null
}

variable "rds_tls_verify" {
  description = <<-EOT
    Whether the tick verifies the RDS TLS chain. Leave true. The gateway image
    ships the RDS CA bundle at the path `src/shared/database.py` expects, so
    there is no reason to disable it.
  EOT
  type        = bool
  default     = true
}

variable "schedule_enabled" {
  description = <<-EOT
    Whether the EventBridge rule is ENABLED. The documented rollback for this
    story is `aws events disable-rule`, which stops the tick in seconds with no
    deploy; this variable is the Terraform-side equivalent.
  EOT
  type        = bool
  default     = true
}

variable "reserved_concurrency" {
  description = <<-EOT
    Reserved concurrent executions. The tick is concurrency-safe by construction
    (every write is guarded on the observed prior state), so this is an RDS
    connection-count bound rather than a correctness mechanism. -1 disables the
    reservation.
  EOT
  type        = number
  default     = 2
}

variable "alert_email_addresses" {
  description = <<-EOT
    Email addresses that receive stall/halt alerts (Issue #4211). Each address must
    be CONFIRMED by its owner before AWS delivers to it, so adding one here is a
    two-step operation: apply, then click the confirmation link.

    Empty is a valid state and is the default: the topic still exists and the engine
    still publishes to it successfully, so nothing fails. What it means is that the
    alert reaches no human — which is why the tick emits `NotificationsFailed` and
    `StallsDetected` as separate metrics, so "detection fired but nobody was
    subscribed" is visible rather than looking healthy.
  EOT
  type        = list(string)
  default     = []
}

# -----------------------------------------------------------------------------
# Engine dispatch (Issue #4313)
# -----------------------------------------------------------------------------
# The tick produces the agent envelope directly onto the existing agent-submit
# FIFO queue, per the ruling in
# docs/design-notes/4303-engine-genesis-transport.md. The queue is owned by a
# DIFFERENT Terraform state (modules/agent-factory/webhook-ingress/infra/), so its
# ARN and URL are passed in as variables rather than referenced or hardcoded —
# the same cross-module ARN-as-variable pattern as
# `var.agent_context_ingestion_queue_arn` in ../../main.tf.

variable "agent_submit_queue_arn" {
  description = <<-EOT
    ARN of the agent-submit FIFO queue the engine dispatches onto. Used to scope
    `sqs:SendMessage` in iam.tf — never `Resource = "*"`.

    Empty falls back to the conventional name `<name_prefix>-agent-submit.fifo` in
    this account and region, so a deploy where the webhook-ingress state has not
    been read still produces a SCOPED policy rather than a wildcard one.
  EOT
  type        = string
  default     = ""
}

variable "agent_submit_queue_url" {
  description = <<-EOT
    URL of the agent-submit FIFO queue, passed to the tick as
    BG_ORCH_DISPATCH_QUEUE_URL.

    Empty is a valid state and is the default: `dispatch_pass.py` reports
    `dispatch_enabled=false` and counts ready nodes as `undispatchable` rather
    than failing, so an unwired environment is VISIBLE as unwired instead of
    looking like an idle one. Nothing is dispatched until this is set.
  EOT
  type        = string
  default     = ""
}

variable "dispatch_repo" {
  description = <<-EOT
    `owner/name` of the repository the engine dispatches work into, passed as
    BG_ORCH_DISPATCH_REPO.

    This is configuration rather than graph state because `OrchestrationNode`
    carries no repo: it stores only `issue_ref` (an issue number), while the agent
    worker's `parse_envelope` requires `source_ref.{installation_id, repo, issue}`.
    See the scope section of `src/orchestration/dispatch_pass.py`.

    Empty means no dispatch happens — same visible-not-silent behaviour as the
    queue URL above.
  EOT
  type        = string
  default     = ""
}

variable "dispatch_persona" {
  description = <<-EOT
    The agent persona engine dispatches as (BG_ORCH_DISPATCH_PERSONA). `developer`
    because a story node is delivery work.

    Configurable but deliberately NOT per-node: persona is not authority (R-O5d),
    and nothing downstream may read it as such.
  EOT
  type        = string
  default     = "developer"
}

variable "dispatch_max_per_tick" {
  description = <<-EOT
    Maximum dispatches per tick (BG_ORCH_DISPATCH_MAX_PER_TICK).

    The one unbounded surface this story bounds deliberately: every dispatch is
    agent capacity and model spend, so dispatching every `ready` node in one pass
    turns a large flow into an unbounded cost spike. The cap DELAYS work rather
    than dropping it — the next tick continues from a stable ordering — and a
    capped pass reports `dispatch_capped=true` so "we ran out of budget" never
    reads as "there was nothing left to do".
  EOT
  type        = number
  default     = 10
}
