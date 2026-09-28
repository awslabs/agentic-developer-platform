# CloudWatch alarm for rate-limit monitoring.
# Fires when any tenant hits rate limits more than 10 times per minute.

resource "aws_cloudwatch_metric_alarm" "rate_limit_alarm" {
  alarm_name          = "adp-${var.environment}-webhook-rate-limit-high"
  alarm_description   = "Rate limit hits > 10/min for webhook ingress — potential abuse or misconfigured tenant"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "RateLimited"
  namespace           = "WebhookIngress"
  period              = 60
  statistic           = "Sum"
  threshold           = 10
  treat_missing_data  = "notBreaching"

  tags = merge(var.tags, {
    Name      = "adp-${var.environment}-webhook-rate-limit-high"
    Module    = "webhook-ingress"
    Component = "monitoring"
  })
}
