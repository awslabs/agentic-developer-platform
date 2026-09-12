# EventBridge delivery failures and Lambda execution failures have different
# retry domains. A successful asynchronous invocation can still fail in Lambda.
resource "aws_sqs_queue" "pricing_delivery_failure" {
  name                      = "${var.name_prefix}-pricing-delivery-failure"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
  tags                      = var.common_tags
}

resource "aws_sqs_queue_policy" "pricing_delivery_failure" {
  queue_url = aws_sqs_queue.pricing_delivery_failure.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowScheduledPricingDeliveryFailure"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.pricing_delivery_failure.arn
        Condition = {
          ArnEquals    = { "aws:SourceArn" = aws_cloudwatch_event_rule.pricing_refresh.arn }
          StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "sqs:*"
        Resource  = aws_sqs_queue.pricing_delivery_failure.arn
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      }
    ]
  })
}

resource "aws_sqs_queue" "pricing_execution_failure" {
  name                      = "${var.name_prefix}-pricing-execution-failure"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
  tags                      = var.common_tags
}

resource "aws_sqs_queue_policy" "pricing_execution_failure" {
  queue_url = aws_sqs_queue.pricing_execution_failure.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "sqs:*"
      Resource  = aws_sqs_queue.pricing_execution_failure.arn
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_lambda_function_event_invoke_config" "pricing_refresh" {
  # The EventBridge target is unqualified; configuring an unused alias would
  # leave the actual invocation path on AWS's defaults.
  function_name                = aws_lambda_function.pricing_refresh.function_name
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = 3600

  destination_config {
    on_failure {
      destination = aws_sqs_queue.pricing_execution_failure.arn
    }
  }

  depends_on = [aws_iam_role_policy.pricing_refresh, aws_sqs_queue_policy.pricing_execution_failure]
}

# No recipient or email confirmation is needed on a new deployment. This is
# an operational inbox that can be inspected by API; it is not human paging.
locals {
  create_pricing_alarm_inbox = length(var.alarm_actions) == 0
  pricing_topic_name         = "${var.name_prefix}-pricing-alarms"
  pricing_topic_arn          = "arn:${data.aws_partition.current.partition}:sns:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:${local.pricing_topic_name}"
  pricing_alarm_actions      = local.create_pricing_alarm_inbox ? [aws_sns_topic.pricing_alarms[0].arn] : var.alarm_actions
}

resource "aws_kms_key" "pricing_alarms" {
  count                   = local.create_pricing_alarm_inbox ? 1 : 0
  description             = "Encrypt pricing alarm notifications; permits CloudWatch publishing"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableAccountKeyAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "AllowCloudWatchAlarmEncryption"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = ["kms:GenerateDataKey*", "kms:Decrypt"]
        Resource  = "*"
        Condition = {
          StringEquals = { "kms:EncryptionContext:aws:sns:topicArn" = local.pricing_topic_arn }
        }
      }
    ]
  })
  tags = var.common_tags
}

resource "aws_sns_topic" "pricing_alarms" {
  count             = local.create_pricing_alarm_inbox ? 1 : 0
  name              = local.pricing_topic_name
  kms_master_key_id = aws_kms_key.pricing_alarms[0].arn
  tags              = var.common_tags
}

resource "aws_sns_topic_policy" "pricing_alarms" {
  count = local.create_pricing_alarm_inbox ? 1 : 0
  arn   = aws_sns_topic.pricing_alarms[0].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowDeploymentAlarmPublication"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.pricing_alarms[0].arn
        Condition = {
          StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
          ArnLike      = { "aws:SourceArn" = "arn:${data.aws_partition.current.partition}:cloudwatch:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:alarm:${var.name_prefix}-*" }
        }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "sns:*"
        Resource  = aws_sns_topic.pricing_alarms[0].arn
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      }
    ]
  })
}

resource "aws_sqs_queue" "pricing_alarm_inbox" {
  count                     = local.create_pricing_alarm_inbox ? 1 : 0
  name                      = "${var.name_prefix}-pricing-alarm-inbox"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
  tags                      = var.common_tags
}

resource "aws_sqs_queue_policy" "pricing_alarm_inbox" {
  count     = local.create_pricing_alarm_inbox ? 1 : 0
  queue_url = aws_sqs_queue.pricing_alarm_inbox[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowPricingAlarmNotifications"
        Effect    = "Allow"
        Principal = { Service = "sns.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.pricing_alarm_inbox[0].arn
        Condition = {
          ArnEquals    = { "aws:SourceArn" = aws_sns_topic.pricing_alarms[0].arn }
          StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "sqs:*"
        Resource  = aws_sqs_queue.pricing_alarm_inbox[0].arn
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      }
    ]
  })
}

resource "aws_sns_topic_subscription" "pricing_alarm_inbox" {
  count     = local.create_pricing_alarm_inbox ? 1 : 0
  topic_arn = aws_sns_topic.pricing_alarms[0].arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.pricing_alarm_inbox[0].arn

  depends_on = [aws_sqs_queue_policy.pricing_alarm_inbox]
}
