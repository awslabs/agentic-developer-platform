# =============================================================================
# DynamoDB Table for Agent Client Metadata (Issue #119)
# =============================================================================
#
# This table stores the mapping between Cognito App Client IDs and their
# organizational context (org_id, team_id, etc.). Used by the Pre Token
# Generation Lambda to inject custom claims into client_credentials tokens.
#

resource "aws_dynamodb_table" "agent_clients" {
  name         = "${var.name_prefix}-agent-clients"
  billing_mode = "PAY_PER_REQUEST" # On-demand capacity for unpredictable workloads

  # Primary key: client_id
  hash_key = "client_id"

  attribute {
    name = "client_id"
    type = "S"
  }

  # GSI for querying agents by organization
  global_secondary_index {
    name            = "org_id-index"
    hash_key        = "org_id"
    projection_type = "ALL"
  }

  attribute {
    name = "org_id"
    type = "S"
  }

  # Enable point-in-time recovery for data protection
  point_in_time_recovery {
    enabled = var.environment == "prod" ? true : false
  }

  # Time-to-live for automatic cleanup (optional)
  # ttl {
  #   attribute_name = "expires_at"
  #   enabled        = true
  # }

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-agent-clients"
    Service = "dynamodb"
    Purpose = "agent-client-metadata"
  })
}

# Output the table name and ARN
output "agent_clients_table_name" {
  description = "Name of the DynamoDB table for agent client metadata"
  value       = aws_dynamodb_table.agent_clients.name
}

output "agent_clients_table_arn" {
  description = "ARN of the DynamoDB table for agent client metadata"
  value       = aws_dynamodb_table.agent_clients.arn
}
