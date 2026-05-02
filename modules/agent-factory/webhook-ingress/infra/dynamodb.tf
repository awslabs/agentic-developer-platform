# =============================================================================
# DynamoDB Tables
# =============================================================================
# Two tables for the webhook ingress layer:
# - webhook-events: audit log of all received webhook events
# - rate-limits: per-tenant rate limiting with TTL-based expiry
#
# NOTE: tenant-registry table was decommissioned in Phase C of tenant-identity
# migration (Issue #377). Tenant resolution now uses the identity-index on the
# organizations table via the gateway admin API.
# =============================================================================

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

  # Auto-cleanup after 30 days (caller sets expires_at = now + 30 days on write)
  ttl {
    attribute_name = "expires_at"
    enabled        = true
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
