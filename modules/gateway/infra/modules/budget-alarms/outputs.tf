output "grace_engaged_alarm_arn" {
  description = "ARN of the grace-window-engaged alarm (budget enforcement allowing unverified requests)"
  value       = aws_cloudwatch_metric_alarm.budget_check_grace_engaged.arn
}

output "unexpected_fault_alarm_arn" {
  description = "ARN of the unexpected-exception alarm (code defect; enforcement failing open)"
  value       = aws_cloudwatch_metric_alarm.budget_check_unexpected_fault.arn
}

output "denying_alarm_arn" {
  description = "ARN of the denying alarm (grace window expired; enforced paths returning 503)"
  value       = aws_cloudwatch_metric_alarm.budget_check_denying.arn
}

output "alarm_names" {
  description = "Names of all budget-enforcement alarms"
  value = [
    aws_cloudwatch_metric_alarm.budget_check_grace_engaged.alarm_name,
    aws_cloudwatch_metric_alarm.budget_check_unexpected_fault.alarm_name,
    aws_cloudwatch_metric_alarm.budget_check_denying.alarm_name,
  ]
}
