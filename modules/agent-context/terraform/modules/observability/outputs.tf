# =============================================================================
# Knowledge Layer Observability Module — Outputs
# =============================================================================

output "dashboard_name" {
  description = "CloudWatch dashboard name"
  value       = aws_cloudwatch_dashboard.knowledge_layer.dashboard_name
}

output "dashboard_arn" {
  description = "CloudWatch dashboard ARN"
  value       = aws_cloudwatch_dashboard.knowledge_layer.dashboard_arn
}

output "dashboard_url" {
  description = "Console URL for the Knowledge Layer dashboard"
  value       = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards/dashboard/adp-${var.environment}-knowledge-layer"
}

output "ingestion_log_group_name" {
  description = "CloudWatch log group name for ingestion pipeline"
  value       = aws_cloudwatch_log_group.ingestion.name
}

output "door_log_group_name" {
  description = "CloudWatch log group name for Door queries"
  value       = aws_cloudwatch_log_group.door.name
}

output "sns_topic_arn" {
  description = "SNS topic ARN for Knowledge Layer alarms"
  value       = aws_sns_topic.kl_alarms.arn
}

output "alarm_arns" {
  description = "ARNs of all Knowledge Layer CloudWatch alarms"
  value = [
    aws_cloudwatch_metric_alarm.kl_rollup_not_promoting.arn,
    aws_cloudwatch_metric_alarm.kl_stage_failing.arn,
    aws_cloudwatch_metric_alarm.kl_zombie_runs.arn,
    aws_cloudwatch_metric_alarm.kl_dlq_backlog.arn,
    aws_cloudwatch_metric_alarm.kl_door_errors.arn,
    aws_cloudwatch_metric_alarm.kl_deepwiki_failures.arn,
  ]
}
