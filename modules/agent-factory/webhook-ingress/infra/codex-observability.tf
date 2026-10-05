# SDK observations are distinct from authoritative Task settlement and billing.
variable "codex_observability_enabled" {
  type    = bool
  default = false
}
variable "codex_alarm_actions" {
  type        = list(string)
  default     = []
  description = "Explicit CloudWatch alarm destinations; empty creates observable alarm state without notifications."
}
locals {
  codex_observability_active = var.enable_agent_otel && var.codex_observability_enabled
  codex_alarm_definitions = {
    repeated_failures = { metric = "adp.codex.run.duration", dimension = "failed", statistic = "SampleCount", threshold = 3,
    description = "Three failed SDK runs in five minutes. Inspect correlated Task events and tool/model receipts; do not replay uncertain effects." }
    unknown_outcome = { metric = "adp.codex.run.duration", dimension = "unknown", statistic = "SampleCount", threshold = 1,
    description = "An SDK run observed an unknown model outcome. Reconcile the authoritative Task model operation and reservation before retrying." }
  }
}
resource "aws_cloudwatch_dashboard" "codex_harness" {
  count          = local.codex_observability_active ? 1 : 0
  dashboard_name = "${local.name_prefix}-codex-harness"
  dashboard_body = jsonencode({ widgets = [
    {
      type       = "text", x = 0, y = 0, width = 24, height = 3,
      properties = { markdown = "SDK telemetry measures observed execution, not authoritative Task settlement or dollar cost. Follow the Task ID and trace into the Task ledger for final outcome, delivery, usage and reservation reconciliation. Missing telemetry is not zero work. The queue below is shared with other personas." }
    },
    {
      type = "metric", x = 0, y = 3, width = 12, height = 6,
      properties = { title = "Observed SDK runs by outcome", region = var.aws_region, period = 300, stat = "SampleCount", view = "timeSeries",
      metrics = [for outcome in ["completed", "failed", "cancelled", "unknown"] : ["ADP/AgentTelemetry", "adp.codex.run.duration", "outcome", outcome]] }
    },
    {
      type = "metric", x = 12, y = 3, width = 12, height = 6,
      properties = { title = "Completed SDK run duration (seconds)", region = var.aws_region, period = 300, view = "timeSeries",
        metrics = [["ADP/AgentTelemetry", "adp.codex.run.duration", "outcome", "completed", { stat = "Average", label = "Average" }],
      ["ADP/AgentTelemetry", "adp.codex.run.duration", "outcome", "completed", { stat = "Maximum", label = "Maximum" }]] }
    },
    {
      type = "metric", x = 0, y = 9, width = 12, height = 6,
      properties = { title = "Model, tool and completion duration (average seconds)", region = var.aws_region, period = 300, stat = "Average", view = "timeSeries",
      metrics = [for kind in ["model", "tool", "completion"] : ["ADP/AgentTelemetry", "adp.agent.operation.duration", "kind", kind, "outcome", "completed"]] }
    },
    {
      type = "metric", x = 12, y = 9, width = 12, height = 6,
      properties = { title = "Shared Task queue oldest message (seconds)", region = var.aws_region, period = 60, stat = "Maximum", view = "timeSeries",
      metrics = [["AWS/SQS", "ApproximateAgeOfOldestMessage", "QueueName", aws_sqs_queue.agent_submit.name]] }
    },
    {
      type = "log", x = 0, y = 15, width = 24, height = 6,
      properties = { title = "Recent Codex execution events and trace references", region = var.aws_region, view = "table",
      query = "SOURCE '${var.otel_collector_log_group}/logs' | filter @message like /adp.codex./ | fields @timestamp, @message | sort @timestamp desc | limit 100" }
    }
  ] })
}
resource "aws_cloudwatch_metric_alarm" "codex_execution" {
  for_each            = local.codex_observability_active ? local.codex_alarm_definitions : {}
  alarm_name          = "${local.name_prefix}-codex-${each.key}"
  alarm_description   = each.value.description
  namespace           = "ADP/AgentTelemetry"
  metric_name         = each.value.metric
  dimensions          = { outcome = each.value.dimension }
  statistic           = each.value.statistic
  period              = 300
  evaluation_periods  = 1
  threshold           = each.value.threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.codex_alarm_actions
}
resource "aws_cloudwatch_metric_alarm" "codex_queue_age" {
  count               = local.codex_observability_active ? 1 : 0
  alarm_name          = "${local.name_prefix}-codex-shared-queue-age"
  alarm_description   = "Shared Task queue has messages older than five minutes. Check worker capacity and admission errors; this queue also serves other personas."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = aws_sqs_queue.agent_submit.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 300
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.codex_alarm_actions
}
