# =============================================================================
# SQS FIFO Queue + Dead-Letter Queue
# =============================================================================
# FIFO ensures ordered processing per issue (MessageGroupId = tenant#repo#issue).
# Content-based deduplication prevents duplicate webhook deliveries.
# Base visibility_timeout is short (~5min) for fast dead-worker detection;
# healthy workers extend via ChangeMessageVisibility heartbeat (#2324).
# =============================================================================

resource "aws_sqs_queue" "agent_submit_dlq" {
  name                        = "${local.name_prefix}-agent-submit-dlq.fifo"
  fifo_queue                  = true
  content_based_deduplication = true
  message_retention_seconds   = 1209600 # 14 days for DLQ inspection
}

resource "aws_sqs_queue" "agent_submit" {
  name                        = "${local.name_prefix}-agent-submit.fifo"
  fifo_queue                  = true
  content_based_deduplication = true
  visibility_timeout_seconds  = var.sqs_visibility_timeout
  message_retention_seconds   = var.sqs_message_retention

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.agent_submit_dlq.arn
    maxReceiveCount     = var.sqs_max_receive_count
  })
}

# Allow the DLQ to receive messages from the main queue
resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.agent_submit_dlq.url

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.agent_submit.arn]
  })
}
