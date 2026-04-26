resource "aws_sqs_queue" "dlq" {
  name                      = "${var.name_prefix}-gateway-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
  tags                      = var.tags
}

resource "aws_sqs_queue" "input" {
  name                       = "${var.name_prefix}-gateway-tasks"
  visibility_timeout_seconds = var.visibility_timeout
  message_retention_seconds  = var.message_retention
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })
  tags = var.tags
}

resource "aws_sqs_queue" "response" {
  # FIFO so AG-UI events (RUN_STARTED → TEXT_MESSAGE_START → CONTENT → END →
  # RUN_FINISHED) arrive at the response Lambda in order and are serialised
  # per session via MessageGroupId = session_id. With Standard, events were
  # delivered out of order — TEXT_MESSAGE_END could arrive after RUN_FINISHED,
  # and the frontend's dedup-on-RUN_FINISHED would drop the later content.
  # The `.fifo` name suffix is required by AWS; producers already detect this
  # via `url.endsWith('.fifo')` and auto-wire MessageGroupId + dedup id.
  name                        = "${var.name_prefix}-gateway-responses.fifo"
  fifo_queue                  = true
  content_based_deduplication = false # producers supply explicit dedup ids
  visibility_timeout_seconds  = 30
  message_retention_seconds   = 86400
  sqs_managed_sse_enabled     = true
  tags                        = var.tags
}

resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${var.name_prefix}-gateway-dlq-alarm"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  tags                = var.tags
}
