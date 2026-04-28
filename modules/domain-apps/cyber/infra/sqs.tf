# =============================================================================
# SQS FIFO Queues — Cyber triage + static analysis task delivery
# =============================================================================
# Issue #230: Copied from modules/agent-factory/infra/chat-agent-infra.tf
# MessageGroupId convention: artifact_id (one sample = one group)
# =============================================================================

# ---------------------------------------------------------------------------
# Triage Tasks Queue + DLQ
# ---------------------------------------------------------------------------

resource "aws_sqs_queue" "cyber_triage_tasks" {
  name                        = "${local.name_prefix}-triage-tasks.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId"
  visibility_timeout_seconds  = 900
  message_retention_seconds   = 345600
  sqs_managed_sse_enabled     = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.cyber_triage_dlq.arn
    maxReceiveCount     = 3
  })

  tags = {
    Component = "cyber-triage"
  }
}

resource "aws_sqs_queue" "cyber_triage_dlq" {
  name                    = "${local.name_prefix}-triage-dlq.fifo"
  fifo_queue              = true
  sqs_managed_sse_enabled = true

  tags = {
    Component = "cyber-triage"
  }
}

# ---------------------------------------------------------------------------
# Triage Responses Queue
# ---------------------------------------------------------------------------

resource "aws_sqs_queue" "cyber_triage_responses" {
  name                        = "${local.name_prefix}-triage-responses.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId"
  visibility_timeout_seconds  = 60
  message_retention_seconds   = 345600
  sqs_managed_sse_enabled     = true

  tags = {
    Component = "cyber-triage"
  }
}

# ---------------------------------------------------------------------------
# Static Tasks Queue + DLQ
# ---------------------------------------------------------------------------

resource "aws_sqs_queue" "cyber_static_tasks" {
  name                        = "${local.name_prefix}-static-tasks.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId"
  visibility_timeout_seconds  = 900
  message_retention_seconds   = 345600
  sqs_managed_sse_enabled     = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.cyber_static_dlq.arn
    maxReceiveCount     = 3
  })

  tags = {
    Component = "cyber-static"
  }
}

resource "aws_sqs_queue" "cyber_static_dlq" {
  name                    = "${local.name_prefix}-static-dlq.fifo"
  fifo_queue              = true
  sqs_managed_sse_enabled = true

  tags = {
    Component = "cyber-static"
  }
}

# ---------------------------------------------------------------------------
# Static Responses Queue
# ---------------------------------------------------------------------------

resource "aws_sqs_queue" "cyber_static_responses" {
  name                        = "${local.name_prefix}-static-responses.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId"
  visibility_timeout_seconds  = 60
  message_retention_seconds   = 345600
  sqs_managed_sse_enabled     = true

  tags = {
    Component = "cyber-static"
  }
}
