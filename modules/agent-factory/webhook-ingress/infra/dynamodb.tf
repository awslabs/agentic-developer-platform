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
}

# -----------------------------------------------------------------------------
# Webhook Events
# PK: event_id (X-GitHub-Delivery header)
# SK: arrived_at (ISO 8601 timestamp)
# GSI: tenant_id for per-tenant queries
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

  global_secondary_index {
    name            = "tenant-index"
    hash_key        = "tenant_id"
    range_key       = "arrived_at"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
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
}
