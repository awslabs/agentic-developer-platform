output "rule_arn" {
  description = "EventBridge rule ARN for dream cycle"
  value       = aws_cloudwatch_event_rule.dream.arn
}

output "rule_name" {
  description = "EventBridge rule name"
  value       = aws_cloudwatch_event_rule.dream.name
}
