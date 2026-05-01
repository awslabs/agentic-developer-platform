# DynamoDB tables for webhook event logging and rate limiting.
# Part of issue #317 (DDB tables) — deployed by this module.

# --- Webhook Events Table ---
# Stores every incoming webhook for audit and observability.
# PK: event_id, SK: arrived_at
# GSI1: tenant_id + arrived_at (query all events for a tenant)
# TTL on expires_at (auto-cleanup after 30 days)

resource "aws_dynamodb_table" "webhook_events" {
  name         = "adp-${var.environment}-webhook-events"
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
    name = "GSI1PK"
    type = "S"
  }

  attribute {
    name = "GSI1SK"
    type = "S"
  }

  global_secondary_index {
    name            = "gsi1"
    hash_key        = "GSI1PK"
    range_key       = "GSI1SK"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(var.tags, {
    Name      = "adp-${var.environment}-webhook-events"
    Module    = "webhook-ingress"
    Component = "observability"
  })
}

# --- Rate Limits Table ---
# Sliding-window counters per tenant for rate limiting.
# PK: tenant_id, SK: window (5-min bucket timestamp)
# TTL on ttl attribute (auto-cleanup after 2 hours)

resource "aws_dynamodb_table" "rate_limits" {
  name         = "adp-${var.environment}-rate-limits"
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

  tags = merge(var.tags, {
    Name      = "adp-${var.environment}-rate-limits"
    Module    = "webhook-ingress"
    Component = "rate-limiting"
  })
}

# --- Outputs ---

output "webhook_events_table_name" {
  description = "Name of the webhook events DynamoDB table"
  value       = aws_dynamodb_table.webhook_events.name
}

output "webhook_events_table_arn" {
  description = "ARN of the webhook events DynamoDB table"
  value       = aws_dynamodb_table.webhook_events.arn
}

output "rate_limits_table_name" {
  description = "Name of the rate limits DynamoDB table"
  value       = aws_dynamodb_table.rate_limits.name
}

output "rate_limits_table_arn" {
  description = "ARN of the rate limits DynamoDB table"
  value       = aws_dynamodb_table.rate_limits.arn
}
