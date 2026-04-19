# =============================================================================
# Complex Task Chat Agent Infrastructure
# =============================================================================
# New resources for the chat agent worker (issue #44):
#   - DynamoDB tables: chat_context, chat_artifacts, agent_memory
#   - S3 bucket for artifacts with 30-day lifecycle
#   - FIFO SQS queue pair for session-serialized task processing
#   - Session sweeper Lambda + DDB stream subscription
#   - IRSA extensions for the gateway-agent role
#   - Separate IAM role for the sweeper Lambda
# =============================================================================

# =============================================================================
# DynamoDB Tables
# =============================================================================

resource "aws_dynamodb_table" "chat_context" {
  name         = "adp-${var.environment}-chat-context"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  stream_enabled   = true
  stream_view_type = "OLD_IMAGE"

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Component = "chat-agent"
  }
}

resource "aws_dynamodb_table" "chat_artifacts" {
  name         = "adp-${var.environment}-chat-artifacts"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  attribute {
    name = "id"
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

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Component = "chat-agent"
  }
}

resource "aws_dynamodb_table" "agent_memory" {
  name         = "adp-${var.environment}-agent-memory"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  attribute {
    name = "id"
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

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Component = "chat-agent"
  }
}

# =============================================================================
# S3 Artifacts Bucket
# =============================================================================

resource "aws_s3_bucket" "chat_artifacts" {
  bucket = "adp-${var.environment}-chat-artifacts-${data.aws_caller_identity.current.account_id}"

  tags = {
    Component = "chat-agent"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "chat_artifacts" {
  bucket = aws_s3_bucket.chat_artifacts.id

  rule {
    id     = "default-30-day-expiry"
    status = "Enabled"

    expiration {
      days = 30
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "chat_artifacts" {
  bucket = aws_s3_bucket.chat_artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "chat_artifacts" {
  bucket = aws_s3_bucket.chat_artifacts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# =============================================================================
# FIFO SQS Queues
# =============================================================================

resource "aws_sqs_queue" "chat_agent_tasks_fifo" {
  name                        = "adp-${var.environment}-agent-gateway-tasks.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId"
  visibility_timeout_seconds  = 900
  message_retention_seconds   = 345600
  sqs_managed_sse_enabled     = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.chat_agent_dlq_fifo.arn
    maxReceiveCount     = 3
  })

  tags = {
    Component = "chat-agent"
  }
}

resource "aws_sqs_queue" "chat_agent_dlq_fifo" {
  name                    = "adp-${var.environment}-agent-gateway-dlq.fifo"
  fifo_queue              = true
  sqs_managed_sse_enabled = true

  tags = {
    Component = "chat-agent"
  }
}

# =============================================================================
# Session Sweeper Lambda
# =============================================================================

# Stub Lambda package (skeleton — real code from the TS build replaces this)
data "archive_file" "session_sweeper_stub" {
  type        = "zip"
  output_path = "${path.module}/lambda-stubs/session-sweeper.zip"

  source {
    content  = "exports.handler = async (event) => { console.log('stub', JSON.stringify(event)); };"
    filename = "index.js"
  }
}

resource "aws_lambda_function" "session_sweeper" {
  function_name = "adp-${var.environment}-chat-session-sweeper"
  runtime       = "nodejs22.x"
  handler       = "index.handler"
  role          = aws_iam_role.session_sweeper.arn
  timeout       = 60
  memory_size   = 256

  filename         = data.archive_file.session_sweeper_stub.output_path
  source_code_hash = data.archive_file.session_sweeper_stub.output_base64sha256

  environment {
    variables = {
      CONTEXT_TABLE       = aws_dynamodb_table.chat_context.name
      ARTIFACTS_TABLE     = aws_dynamodb_table.chat_artifacts.name
      ARTIFACTS_BUCKET    = aws_s3_bucket.chat_artifacts.id
      AWS_REGION_OVERRIDE = var.aws_region
    }
  }

  tags = {
    Component = "chat-agent"
  }
}

resource "aws_lambda_event_source_mapping" "session_sweeper" {
  event_source_arn  = aws_dynamodb_table.chat_context.stream_arn
  function_name     = aws_lambda_function.session_sweeper.arn
  starting_position = "LATEST"
  batch_size        = 10

  filter_criteria {
    filter {
      pattern = jsonencode({
        eventName = ["REMOVE"]
        dynamodb  = { Keys = { SK = { S = ["header"] } } }
      })
    }
  }
}

# =============================================================================
# Sweeper Lambda IAM Role (narrow scope — no Bedrock, no SQS)
# =============================================================================

resource "aws_iam_role" "session_sweeper" {
  name = "adp-${var.environment}-chat-session-sweeper-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    Component = "chat-agent"
  }
}

resource "aws_iam_role_policy" "session_sweeper_dynamodb" {
  name = "dynamodb-cleanup"
  role = aws_iam_role.session_sweeper.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadContextStream"
        Effect = "Allow"
        Action = [
          "dynamodb:GetRecords",
          "dynamodb:GetShardIterator",
          "dynamodb:DescribeStream",
          "dynamodb:ListStreams"
        ]
        Resource = "${aws_dynamodb_table.chat_context.arn}/stream/*"
      },
      {
        Sid    = "CleanupTables"
        Effect = "Allow"
        Action = [
          "dynamodb:Query",
          "dynamodb:BatchWriteItem",
          "dynamodb:DeleteItem"
        ]
        Resource = [
          aws_dynamodb_table.chat_context.arn,
          "${aws_dynamodb_table.chat_context.arn}/index/*",
          aws_dynamodb_table.chat_artifacts.arn,
          "${aws_dynamodb_table.chat_artifacts.arn}/index/*",
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy" "session_sweeper_s3" {
  name = "s3-cleanup"
  role = aws_iam_role.session_sweeper.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "s3:DeleteObject",
        "s3:ListBucket"
      ]
      Resource = [
        aws_s3_bucket.chat_artifacts.arn,
        "${aws_s3_bucket.chat_artifacts.arn}/*"
      ]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "session_sweeper_logs" {
  role       = aws_iam_role.session_sweeper.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# =============================================================================
# IRSA Extensions — chat agent permissions on gateway-agent role
# =============================================================================

resource "aws_iam_role_policy" "gateway_agent_chat_dynamodb" {
  name = "chat-dynamodb-access"
  role = aws_iam_role.gateway_agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
        "dynamodb:Query",
        "dynamodb:BatchGetItem",
        "dynamodb:BatchWriteItem",
        "dynamodb:TransactWriteItems"
      ]
      Resource = [
        aws_dynamodb_table.chat_context.arn,
        "${aws_dynamodb_table.chat_context.arn}/index/*",
        aws_dynamodb_table.chat_artifacts.arn,
        "${aws_dynamodb_table.chat_artifacts.arn}/index/*",
        aws_dynamodb_table.agent_memory.arn,
        "${aws_dynamodb_table.agent_memory.arn}/index/*",
      ]
    }]
  })
}

resource "aws_iam_role_policy" "gateway_agent_chat_s3" {
  name = "chat-artifacts-s3"
  role = aws_iam_role.gateway_agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ]
      Resource = [
        aws_s3_bucket.chat_artifacts.arn,
        "${aws_s3_bucket.chat_artifacts.arn}/*"
      ]
    }]
  })
}

resource "aws_iam_role_policy" "gateway_agent_chat_sqs_fifo" {
  name = "chat-sqs-fifo"
  role = aws_iam_role.gateway_agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ConsumeFIFOTasks"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ]
        Resource = aws_sqs_queue.chat_agent_tasks_fifo.arn
      },
      {
        Sid    = "SendFIFOResponses"
        Effect = "Allow"
        Action = ["sqs:SendMessage"]
        # Chat responses go to the existing response queue (non-FIFO)
        Resource = module.gateway_sqs.response_queue_arn
      }
    ]
  })
}

# =============================================================================
# Outputs
# =============================================================================

output "chat_context_table_name" {
  description = "DynamoDB table for chat context"
  value       = aws_dynamodb_table.chat_context.name
}

output "chat_context_table_arn" {
  description = "DynamoDB table ARN for chat context"
  value       = aws_dynamodb_table.chat_context.arn
}

output "chat_artifacts_table_name" {
  description = "DynamoDB table for chat artifacts catalog"
  value       = aws_dynamodb_table.chat_artifacts.name
}

output "agent_memory_table_name" {
  description = "DynamoDB table for cross-agent memory"
  value       = aws_dynamodb_table.agent_memory.name
}

output "chat_artifacts_bucket" {
  description = "S3 bucket for chat artifacts"
  value       = aws_s3_bucket.chat_artifacts.id
}

output "chat_tasks_fifo_queue_url" {
  description = "FIFO SQS queue URL for chat agent tasks"
  value       = aws_sqs_queue.chat_agent_tasks_fifo.url
}

output "chat_tasks_fifo_queue_arn" {
  description = "FIFO SQS queue ARN for chat agent tasks"
  value       = aws_sqs_queue.chat_agent_tasks_fifo.arn
}

output "session_sweeper_lambda_arn" {
  description = "Session sweeper Lambda ARN"
  value       = aws_lambda_function.session_sweeper.arn
}
