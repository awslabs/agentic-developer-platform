resource "aws_sqs_queue" "dead_letter" {
  name                      = "${local.name_prefix}-work-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_sqs_queue" "work" {
  name                       = "${local.name_prefix}-work"
  delay_seconds              = 0
  visibility_timeout_seconds = var.service_id == "security" ? 5400 : (var.service_id == "devops" ? 720 : 180)
  message_retention_seconds  = 345600
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dead_letter.arn
    maxReceiveCount     = 5
  })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_lambda_event_source_mapping" "work" {
  event_source_arn        = aws_sqs_queue.work.arn
  function_name           = aws_lambda_function.worker.arn
  depends_on              = [aws_lambda_function_recursion_config.worker]
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]
  enabled                 = false

  lifecycle {
    ignore_changes = [enabled]
  }
}

resource "aws_cloudwatch_metric_alarm" "dead_letters" {
  alarm_name          = "${local.name_prefix}-dlq"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.dead_letter.name }
}
resource "aws_cloudwatch_metric_alarm" "work_age" {
  alarm_name          = "${local.name_prefix}-work-age"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "ApproximateAgeOfOldestMessage"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 900
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.work.name }
}
