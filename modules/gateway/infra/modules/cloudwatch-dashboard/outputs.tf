output "dashboard_arn" {
  description = "ARN of the CloudWatch latency dashboard"
  value       = aws_cloudwatch_dashboard.latency.dashboard_arn
}

output "dashboard_name" {
  description = "Name of the CloudWatch latency dashboard"
  value       = aws_cloudwatch_dashboard.latency.dashboard_name
}
