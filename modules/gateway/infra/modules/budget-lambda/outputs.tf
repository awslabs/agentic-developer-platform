# =============================================================================
# Outputs for Budget Lambda Module (Issue #234)
# =============================================================================

# Usage Tracker Lambda
output "usage_tracker_lambda_arn" {
  description = "ARN of the budget usage tracker Lambda function"
  value       = aws_lambda_function.usage_tracker.arn
}

output "usage_tracker_lambda_name" {
  description = "Name of the budget usage tracker Lambda function"
  value       = aws_lambda_function.usage_tracker.function_name
}

output "usage_tracker_lambda_invoke_arn" {
  description = "Invoke ARN of the budget usage tracker Lambda function"
  value       = aws_lambda_function.usage_tracker.invoke_arn
}

# Pricing Refresh Lambda
output "pricing_refresh_lambda_arn" {
  description = "ARN of the pricing refresh Lambda function"
  value       = aws_lambda_function.pricing_refresh.arn
}

output "pricing_refresh_lambda_name" {
  description = "Name of the pricing refresh Lambda function"
  value       = aws_lambda_function.pricing_refresh.function_name
}

output "pricing_refresh_lambda_invoke_arn" {
  description = "Invoke ARN of the pricing refresh Lambda function"
  value       = aws_lambda_function.pricing_refresh.invoke_arn
}

# Security Group
output "lambda_security_group_id" {
  description = "Security group ID for budget Lambda functions"
  value       = aws_security_group.lambda.id
}

# CloudWatch Log Groups
output "usage_tracker_log_group_name" {
  description = "CloudWatch log group name for usage tracker Lambda"
  value       = aws_cloudwatch_log_group.usage_tracker.name
}

output "pricing_refresh_log_group_name" {
  description = "CloudWatch log group name for pricing refresh Lambda"
  value       = aws_cloudwatch_log_group.pricing_refresh.name
}

# EventBridge Schedule
output "pricing_refresh_schedule_rule_arn" {
  description = "ARN of the EventBridge schedule rule for pricing refresh"
  value       = aws_cloudwatch_event_rule.pricing_refresh.arn
}

output "pricing_refresh_schedule_rule_name" {
  value = aws_cloudwatch_event_rule.pricing_refresh.name
}

output "pricing_delivery_failure_queue" {
  description = "EventBridge delivery failures; separate from Lambda execution failures."
  value       = { arn = aws_sqs_queue.pricing_delivery_failure.arn, url = aws_sqs_queue.pricing_delivery_failure.url }
}

output "pricing_execution_failure_queue" {
  description = "Lambda asynchronous execution failures."
  value       = { arn = aws_sqs_queue.pricing_execution_failure.arn, url = aws_sqs_queue.pricing_execution_failure.url }
}

output "pricing_alarm_topic_arns" {
  value = local.pricing_alarm_actions
}

output "pricing_alarm_inbox" {
  description = "Default SNS-subscribed operational inbox; null when existing topics were supplied."
  value = local.create_pricing_alarm_inbox ? {
    arn              = aws_sqs_queue.pricing_alarm_inbox[0].arn
    url              = aws_sqs_queue.pricing_alarm_inbox[0].url
    subscription_arn = aws_sns_topic_subscription.pricing_alarm_inbox[0].arn
    kms_key_arn      = aws_kms_key.pricing_alarms[0].arn
  } : null
}

# CloudWatch Alarms
output "unknown_model_pricing_alarm_arn" {
  description = "ARN of the unknown-model-pricing alarm (model billed at default fallback rate — Issue #4592)"
  value       = aws_cloudwatch_metric_alarm.unknown_model_pricing.arn
}

# Lambda Layer
output "psycopg2_layer_arn" {
  description = "ARN of the psycopg2 Lambda Layer"
  value       = aws_lambda_layer_version.psycopg2.arn
}
