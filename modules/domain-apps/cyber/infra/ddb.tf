# =============================================================================
# DynamoDB — Cyber analysis results table
# =============================================================================
# Issue #230: Copied from modules/agent-factory/infra/chat-agent-infra.tf
# PK = artifact_id, SK = stage#timestamp
# GSIs: by-id (id), by-org (org_id, SK)
# =============================================================================

resource "aws_dynamodb_table" "cyber_analysis_results" {
  name         = "${local.name_prefix}-analysis-results"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "artifact_id"
  range_key    = "stage_timestamp"

  attribute {
    name = "artifact_id"
    type = "S"
  }

  attribute {
    name = "stage_timestamp"
    type = "S"
  }

  attribute {
    name = "id"
    type = "S"
  }

  attribute {
    name = "org_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  global_secondary_index {
    name            = "by-id"
    hash_key        = "id"
    projection_type = "ALL"
  }

  global_secondary_index {
    name            = "by-org"
    hash_key        = "org_id"
    range_key       = "stage_timestamp"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.dynamodb.arn
  }

  tags = {
    Component = "cyber-analysis"
  }
}
