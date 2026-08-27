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
