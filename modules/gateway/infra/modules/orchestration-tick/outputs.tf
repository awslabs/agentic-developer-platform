# =============================================================================
# Outputs for the Orchestration Tick module (Issue #4203)
# =============================================================================

output "tick_lambda_arn" {
  description = "ARN of the orchestration tick Lambda function"
  value       = aws_lambda_function.tick.arn
}

output "tick_lambda_name" {
  description = "Name of the orchestration tick Lambda function (also the log group suffix)"
  value       = aws_lambda_function.tick.function_name
}

output "tick_schedule_rule_name" {
  description = "Name of the EventBridge rule that fires the tick (used by the deploy smoke check)"
  value       = aws_cloudwatch_event_rule.tick.name
}

output "tick_schedule_rule_arn" {
  description = "ARN of the EventBridge rule that fires the tick"
  value       = aws_cloudwatch_event_rule.tick.arn
}

output "tick_log_group_name" {
  description = "CloudWatch log group the tick writes its `tick_report` line to"
  value       = aws_cloudwatch_log_group.tick.name
}

output "tick_security_group_id" {
  description = "Security group ID of the orchestration tick Lambda"
  value       = aws_security_group.tick.id
}

output "tick_role_arn" {
  description = "ARN of the orchestration tick Lambda execution role"
  value       = aws_iam_role.tick.arn
}
