terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "name_prefix" {
  type = string
}

variable "worker_function_arn" {
  type = string
}

variable "repair_function_arn" {
  type = string
}

variable "event_ingest_function_arn" {
  type = string
}

variable "readback_function_arn" {
  type = string
}

variable "native_event_pattern" {
  type = string

  validation {
    condition     = can(jsondecode(var.native_event_pattern))
    error_message = "The approved native EventBridge pattern must be JSON."
  }
}

variable "gateway_role_name" {
  type = string
}

variable "worker_role_name" {
  type = string
}

variable "producer_role_names" {
  type = set(string)
}

variable "artifact_bucket_arn" {
  type = string
}

variable "artifact_kms_key_arn" {
  type = string
}

variable "secret_reference_arns" {
  type    = set(string)
  default = []
}

variable "alarm_actions" {
  type    = list(string)
  default = []
}

variable "tags" {
  type    = map(string)
  default = {}
}

resource "aws_sqs_queue" "devops_dead_letters" {
  name                      = "${var.name_prefix}-devops-dlq"
  sqs_managed_sse_enabled   = true
  message_retention_seconds = 1209600
  tags                      = var.tags
}

resource "aws_sqs_queue" "devops_work" {
  name                       = "${var.name_prefix}-devops-work"
  visibility_timeout_seconds = 900
  message_retention_seconds  = 1209600
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.devops_dead_letters.arn
    maxReceiveCount     = 5
  })
  tags = var.tags
}

resource "aws_lambda_event_source_mapping" "devops_worker" {
  event_source_arn        = aws_sqs_queue.devops_work.arn
  function_name           = var.worker_function_arn
  batch_size              = 10
  function_response_types = ["ReportBatchItemFailures"]
  enabled                 = false
}

resource "aws_cloudwatch_event_rule" "repair" {
  name                = "${var.name_prefix}-devops-repair"
  schedule_expression = "rate(1 minute)"
  state               = "DISABLED"
  tags                = var.tags
}

resource "aws_cloudwatch_event_rule" "readback" {
  name                = "${var.name_prefix}-devops-readback"
  schedule_expression = "rate(5 minutes)"
  state               = "DISABLED"
  tags                = var.tags
}

resource "aws_cloudwatch_event_rule" "native" {
  name          = "${var.name_prefix}-devops-native"
  event_pattern = var.native_event_pattern
  state         = "DISABLED"
  tags          = var.tags
}

resource "aws_cloudwatch_event_target" "repair" {
  rule = aws_cloudwatch_event_rule.repair.name
  arn  = var.repair_function_arn
}

resource "aws_cloudwatch_event_target" "readback" {
  rule = aws_cloudwatch_event_rule.readback.name
  arn  = var.readback_function_arn
}

resource "aws_cloudwatch_event_target" "native" {
  rule = aws_cloudwatch_event_rule.native.name
  arn  = var.event_ingest_function_arn
}

resource "aws_lambda_permission" "repair" {
  statement_id  = "DevOpsRepairEvents"
  action        = "lambda:InvokeFunction"
  function_name = var.repair_function_arn
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.repair.arn
}

resource "aws_lambda_permission" "readback" {
  statement_id  = "DevOpsReadbackEvents"
  action        = "lambda:InvokeFunction"
  function_name = var.readback_function_arn
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.readback.arn
}

resource "aws_lambda_permission" "native" {
  statement_id  = "DevOpsNativeEvents"
  action        = "lambda:InvokeFunction"
  function_name = var.event_ingest_function_arn
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.native.arn
}

resource "aws_iam_role_policy" "gateway_publish" {
  name = "${var.name_prefix}-devops-publish"
  role = var.gateway_role_name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:SendMessage"]
      Resource = aws_sqs_queue.devops_work.arn
    }]
  })
}

resource "aws_iam_role_policy" "producer_publish" {
  for_each = var.producer_role_names
  name     = "${var.name_prefix}-devops-producer"
  role     = each.value
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.devops_work.arn }]
  })
}

resource "aws_iam_role_policy" "worker" {
  name = "${var.name_prefix}-devops-worker"
  role = var.worker_role_name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes", "sqs:SendMessage"]
        Resource = aws_sqs_queue.devops_work.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${var.artifact_bucket_arn}/devops/*"
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.artifact_kms_key_arn
      }
      ], length(var.secret_reference_arns) == 0 ? [] : [{
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = sort(tolist(var.secret_reference_arns))
    }])
  })
}

resource "aws_cloudwatch_metric_alarm" "dead_letters" {
  alarm_name          = "${var.name_prefix}-devops-dlq"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.devops_dead_letters.name }
  alarm_actions       = var.alarm_actions
  tags                = var.tags
}

resource "aws_cloudwatch_metric_alarm" "work_age" {
  alarm_name          = "${var.name_prefix}-devops-work-age"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "ApproximateAgeOfOldestMessage"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 900
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.devops_work.name }
  alarm_actions       = var.alarm_actions
  tags                = var.tags
}

output "devops_work_queue_arn" {
  value = aws_sqs_queue.devops_work.arn
}

output "devops_work_queue_url" {
  value = aws_sqs_queue.devops_work.url
}

output "devops_dead_letter_queue_arn" {
  value = aws_sqs_queue.devops_dead_letters.arn
}
