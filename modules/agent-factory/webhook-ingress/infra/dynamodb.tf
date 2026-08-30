# =============================================================================
# DynamoDB Tables
# =============================================================================
# Three tables for the webhook ingress layer:
# - tenant-registry: maps GitHub App installations to tenants
# - webhook-events: audit log of all received webhook events
# - rate-limits: per-tenant rate limiting with TTL-based expiry
# =============================================================================

# -----------------------------------------------------------------------------
# Tenant Registry
# PK: installation_id (GitHub App installation ID)
# -----------------------------------------------------------------------------
resource "aws_dynamodb_table" "tenant_registry" {
  name         = "${local.name_prefix}-tenant-registry"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "installation_id"

  attribute {
    name = "installation_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.dynamodb.arn
  }
}

# -----------------------------------------------------------------------------
# Webhook Events
# PK: event_id (X-GitHub-Delivery header)
# SK: arrived_at (ISO 8601 timestamp)
# GSI: tenant_id for per-tenant queries
# GSI: engine_command_status (sparse) for the engine-command bridge (#4527)
# -----------------------------------------------------------------------------
resource "aws_dynamodb_table" "webhook_events" {
  name         = "${local.name_prefix}-webhook-events"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "event_id"
  range_key    = "arrived_at"

  attribute {
    name = "event_id"
    type = "S"
  }

  attribute {
    name = "arrived_at"
    type = "S"
  }

  attribute {
    name = "tenant_id"
    type = "S"
  }

  attribute {
    name = "user_id"
    type = "S"
  }

  attribute {
    name = "correlation_id"
    type = "S"
  }

  attribute {
    name = "root_human_id"
    type = "S"
  }

  attribute {
    name = "engine_command_status"
    type = "S"
  }

  # correlation-index powers the Agent Activity chain view (#1616): retrieve all
  # invocations sharing a correlation_id via a Query (was a full-table Scan,
  # which is costly and required a dynamodb:Scan grant the gateway role lacks).
  global_secondary_index {
    name            = "correlation-index"
    hash_key        = "correlation_id"
    range_key       = "arrived_at"
    projection_type = "ALL"
  }

  global_secondary_index {
    name            = "tenant-index"
    hash_key        = "tenant_id"
    range_key       = "arrived_at"
    projection_type = "ALL"
  }

  global_secondary_index {
    name            = "user-index"
    hash_key        = "user_id"
    range_key       = "arrived_at"
    projection_type = "ALL"
  }

  # root-human-index: sparse GSI for chain runs attributed to a human user.
  # Issue #3705: Agent-spawned chain runs carry user_id = <bot>, but
  # root_human_id = <the human who caused the chain>. This index lets the
  # stats + activity services include those runs in /me queries. Sparse by
  # nature: only rows with root_human_id are indexed.
  global_secondary_index {
    name            = "root-human-index"
    hash_key        = "root_human_id"
    range_key       = "arrived_at"
    projection_type = "ALL"
  }

  # engine-command-index: sparse GSI carrying only `@agent-engine` comments that
  # the orchestration tick has not yet consumed (#4527). The webhook Lambda writes
  # engine_command_status = "pending"; the tick Queries this index oldest-first,
  # applies each command and conditionally flips the attribute to "consumed".
  #
  # Sparse is the point, not a side effect: the table holds 30 days of every
  # webhook delivery, and only rows carrying this attribute are projected, so the
  # tick reads a handful of outstanding commands per wake instead of paginating the
  # whole table. Rows that were never engine commands are invisible here.
  #
  # Hash key is the STATUS rather than the tenant deliberately — the tick is a
  # single scheduled process serving all tenants, and a tenant-keyed index would
  # force it to know the tenant list before it could find any work. Tenant
  # isolation is enforced where the command is applied (org resolved server-side
  # from the installation), never by the shape of this index.
  global_secondary_index {
    name            = "engine-command-index"
    hash_key        = "engine_command_status"
    range_key       = "arrived_at"
    projection_type = "ALL"
  }

  # Auto-cleanup after 30 days (caller sets expires_at = now + 30 days on write)
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.dynamodb.arn
  }
}

# -----------------------------------------------------------------------------
# Rate Limits
# PK: tenant_id
# SK: window (e.g., "2026-05-01T20:00")
# TTL on `ttl` attribute for automatic cleanup
# -----------------------------------------------------------------------------
resource "aws_dynamodb_table" "rate_limits" {
  name         = "${local.name_prefix}-rate-limits"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "tenant_id"
  range_key    = "window"

  attribute {
    name = "tenant_id"
    type = "S"
  }

  attribute {
    name = "window"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.dynamodb.arn
  }
}

# -----------------------------------------------------------------------------
# Correlation Pointers
# PK: channel_key (e.g. "github:repo=aws-e/adp,issue=775" or ".../pr=778")
# Stores only the latest correlation pointer per channel (upsert via PutItem).
# TTL = 7 days from last activity (Phase 2-c refreshes expires_at on each write).
# Issue #784: Phase 2-a storage primitive for correlation propagation.
# -----------------------------------------------------------------------------
resource "aws_dynamodb_table" "correlation_pointers" {
  name         = "${local.name_prefix}-correlation-pointers"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "channel_key"

  attribute {
    name = "channel_key"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.dynamodb.arn
  }
}
