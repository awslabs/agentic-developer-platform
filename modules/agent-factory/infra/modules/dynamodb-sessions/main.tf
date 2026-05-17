resource "aws_dynamodb_table" "sessions" {
  name         = "${var.name_prefix}-gateway-sessions"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "session_id"

  attribute {
    name = "session_id"
    type = "S"
  }

  attribute {
    name = "user_workspace"
    type = "S"
  }

  global_secondary_index {
    name            = "user-workspace-index"
    hash_key        = "user_workspace"
    range_key       = "session_id"
    projection_type = "ALL"
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
    kms_key_arn = var.kms_key_arn
  }

  tags = var.tags
}
