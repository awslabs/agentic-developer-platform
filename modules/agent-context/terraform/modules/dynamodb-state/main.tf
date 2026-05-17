# DynamoDB table for ingestion state tracking
# Replaces repo-state.json with a scalable, queryable state store
#
# PK: source (e.g., "repo#aws-samples/bedrock-chat")
# SK: record_type ("STATE" or "RUN#2026-04-17T06:00:00Z")

resource "aws_dynamodb_table" "ingestion_state" {
  name         = var.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "source"
  range_key    = "record_type"

  attribute {
    name = "source"
    type = "S"
  }

  attribute {
    name = "record_type"
    type = "S"
  }

  attribute {
    name = "content_type"
    type = "S"
  }

  attribute {
    name = "updated_at"
    type = "S"
  }

  attribute {
    name = "graphrag_status"
    type = "S"
  }

  # GSI: Query by content_type (e.g., "show all repos" or "show all failed docs")
  global_secondary_index {
    name            = "content_type-index"
    hash_key        = "content_type"
    range_key       = "updated_at"
    projection_type = "ALL"
  }

  # GSI: Query by graphrag_status (e.g., "show all repos pending GraphRAG")
  global_secondary_index {
    name            = "status-index"
    hash_key        = "graphrag_status"
    range_key       = "updated_at"
    projection_type = "ALL"
  }

  # TTL for RUN records (append-only, expire after 30 days)
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = var.enable_pitr
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }

  tags = merge(var.tags, {
    Component = "ingestion-state"
  })
}

# IAM policy for reading/writing the state table
resource "aws_iam_policy" "dynamodb_readwrite" {
  name        = "${var.table_name}-readwrite"
  description = "Read/write access to the ingestion state DynamoDB table"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:BatchGetItem",
          "dynamodb:BatchWriteItem",
        ]
        Resource = [
          aws_dynamodb_table.ingestion_state.arn,
          "${aws_dynamodb_table.ingestion_state.arn}/index/*",
        ]
      },
      {
        Sid    = "DynamoDBKMSAccess"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = [var.kms_key_arn]
      }
    ]
  })
}

# Attach to the existing IRSA role
resource "aws_iam_role_policy_attachment" "dynamodb_readwrite" {
  count      = var.irsa_role_name != "" ? 1 : 0
  role       = var.irsa_role_name
  policy_arn = aws_iam_policy.dynamodb_readwrite.arn
}
