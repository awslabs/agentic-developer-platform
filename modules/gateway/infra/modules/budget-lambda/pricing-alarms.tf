locals {
  refresh_metric_dimensions = { FunctionName = aws_lambda_function.pricing_refresh.function_name }
  pricing_failure_queues = {
    delivery  = aws_sqs_queue.pricing_delivery_failure.name
    execution = aws_sqs_queue.pricing_execution_failure.name
  }
  # This is the immutable deployment baseline, not a second pricing inventory.
  pricing_source_models = {
    for model_id, model in jsondecode(file("${local.pricing_policy_dir}/snapshots/2026-09-12.1.json")).models :
    model_id => model.source_kind
  }
}

resource "aws_cloudwatch_metric_alarm" "pricing_eventbridge_failure" {
  for_each            = toset(["FailedInvocations", "InvocationsFailedToBeSentToDlq"])
  alarm_name          = "${var.name_prefix}-pricing-events-${each.key}"
  alarm_description   = "EventBridge failed to deliver a pricing refresh or its delivery-failure record. Inspect the rule and delivery queue."
  namespace           = "AWS/Events"
  metric_name         = each.key
  dimensions          = { RuleName = aws_cloudwatch_event_rule.pricing_refresh.name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.pricing_alarm_actions
  tags                = var.common_tags
}

resource "aws_cloudwatch_metric_alarm" "pricing_lambda_failure" {
  for_each            = toset(["Errors", "AsyncEventsDropped", "DestinationDeliveryFailures"])
  alarm_name          = "${var.name_prefix}-pricing-lambda-${each.key}"
  alarm_description   = "Pricing refresh execution or asynchronous failure delivery failed. Inspect Lambda logs and the execution-failure queue."
  namespace           = "AWS/Lambda"
  metric_name         = each.key
  dimensions          = local.refresh_metric_dimensions
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.pricing_alarm_actions
  tags                = var.common_tags
}

resource "aws_cloudwatch_metric_alarm" "pricing_failure_queue_depth" {
  for_each            = local.pricing_failure_queues
  alarm_name          = "${var.name_prefix}-pricing-${each.key}-queue-depth"
  alarm_description   = "Pricing failure records need investigation; inspect without deleting or replaying billing history."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = each.value }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.pricing_alarm_actions
  tags                = var.common_tags
}

resource "aws_cloudwatch_metric_alarm" "pricing_failure_queue_age" {
  for_each            = local.pricing_failure_queues
  alarm_name          = "${var.name_prefix}-pricing-${each.key}-queue-age"
  alarm_description   = "A pricing failure record has remained unresolved for more than one hour."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = each.value }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 3600
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.pricing_alarm_actions
  tags                = var.common_tags
}

resource "aws_cloudwatch_metric_alarm" "pricing_refresh_anomaly" {
  for_each = toset([
    "PricingRequiredVariantsMissing", "PricingVariantsRetained", "PricingRefreshPartial",
    "PricingRefreshRejected", "PricingRefreshDeferred",
  ])
  alarm_name          = "${var.name_prefix}-${each.key}"
  alarm_description   = "Pricing refresh did not fully verify the required inventory. Inspect refresh logs, active generation and retained source ages."
  namespace           = "ADP/Gateway"
  metric_name         = each.key
  dimensions          = local.refresh_metric_dimensions
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.pricing_alarm_actions
  tags                = var.common_tags
}

resource "aws_cloudwatch_metric_alarm" "pricing_oldest_source" {
  alarm_name          = "${var.name_prefix}-pricing-oldest-source"
  alarm_description   = "The oldest required pricing source verification exceeds 48 hours, or its daily freshness measurement is absent."
  namespace           = "ADP/Gateway"
  metric_name         = "PricingOldestVerifiedAgeHours"
  dimensions          = local.refresh_metric_dimensions
  statistic           = "Maximum"
  period              = 86400
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 48
  treat_missing_data  = "breaching"
  alarm_actions       = local.pricing_alarm_actions
  tags                = var.common_tags
}

resource "aws_cloudwatch_metric_alarm" "pricing_source_age" {
  for_each          = local.pricing_source_models
  alarm_name        = "${var.name_prefix}-pricing-source-age-${each.key}"
  alarm_description = "The oldest required variant for this model/source has not been verified within 48 hours, or its daily measurement is missing."
  namespace         = "ADP/Gateway"
  metric_name       = "PricingSourceVerifiedAgeHours"
  dimensions = merge(local.refresh_metric_dimensions, {
    ModelId = each.key
    Source  = each.value
  })
  statistic           = "Maximum"
  period              = 86400
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 48
  treat_missing_data  = "breaching"
  alarm_actions       = local.pricing_alarm_actions
  tags                = var.common_tags
}

resource "aws_cloudwatch_metric_alarm" "pricing_full_refresh_missing" {
  alarm_name          = "${var.name_prefix}-pricing-full-refresh-missing"
  alarm_description   = "No fully fresh pricing publication in 30 hours. Partial publications do not reset this heartbeat."
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  evaluation_periods  = 30
  datapoints_to_alarm = 30
  treat_missing_data  = "breaching"
  alarm_actions       = local.pricing_alarm_actions
  tags                = var.common_tags

  # Fill each empty hour rather than allowing CloudWatch's extra lookback
  # datapoints to reuse an older success and postpone a missing-heartbeat alarm.
  metric_query {
    id          = "heartbeat"
    expression  = "FILL(success, 0)"
    return_data = true
  }
  metric_query {
    id          = "success"
    return_data = false
    metric {
      namespace   = "ADP/Gateway"
      metric_name = "PricingRefreshSuccess"
      dimensions  = local.refresh_metric_dimensions
      period      = 3600
      stat        = "Sum"
    }
  }
}

# Consumer events are fleet-level, dimensionless like UnknownModelPricing. The
# release reviewer must verify these names against the deployed consumer emitters.
resource "aws_cloudwatch_metric_alarm" "pricing_consumer_anomaly" {
  for_each            = toset(["PricingCacheRefreshFailure", "PricingUnknownVariant", "PricingStaleRate"])
  alarm_name          = "${var.name_prefix}-${each.key}"
  alarm_description   = "A pricing consumer used stale or unverified pricing evidence; inspect gateway/tracker pricing logs."
  namespace           = "ADP/Gateway"
  metric_name         = each.key
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.pricing_alarm_actions
  tags                = var.common_tags
}

resource "aws_cloudwatch_metric_alarm" "pricing_consumer_cache_age" {
  alarm_name          = "${var.name_prefix}-pricing-consumer-cache-age"
  alarm_description   = "A consumer's pricing cache refresh has been failing for at least 30 minutes."
  namespace           = "ADP/Gateway"
  metric_name         = "PricingCacheAgeSeconds"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1800
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.pricing_alarm_actions
  tags                = var.common_tags
}
