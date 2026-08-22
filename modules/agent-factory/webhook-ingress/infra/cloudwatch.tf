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

# =============================================================================
# Agent bootstrap log group (issue #4028)
# =============================================================================
# The agent worker writes step-level Setup/bootstrap logs here so bootstrap
# failures remain diagnosable after KEDA garbage-collects the pod
# (agent-worker-image/lib/bootstrap_logger.py:174).
#
# Declared in Terraform rather than left to the worker's runtime CreateLogGroup
# so that (a) retention is fixed at this module's 14-day convention instead of
# the worker's hardcoded 7 (see the PutRetentionPolicy note in
# scaledjob-iam.tf), and (b) the group is CMK-encrypted and visible to checkov —
# a worker-created group would be unencrypted and outside state.
#
# The worker still holds logs:CreateLogGroup (scaledjob-iam.tf) on purpose: it
# calls CreateLogGroup unconditionally and only tolerates
# ResourceAlreadyExistsException, so revoking it would disable CloudWatch
# bootstrap logging for the entire run.
#
# NOTE: kms_key_id here requires no extra KMS permission on the agent-worker
# role. For CMK-encrypted log groups the CloudWatch Logs *service* performs the
# encryption under its own key-policy grant (kms.tf CloudWatchLogsService
# statement, ArnLike on log-group:*), not the PutLogEvents caller.
resource "aws_cloudwatch_log_group" "agent_bootstrap" {
  name              = "/adp/${var.environment}/agent-factory/bootstrap"
  retention_in_days = 14
  kms_key_id        = aws_kms_key.cloudwatch.arn

  tags = merge(var.tags, {
    Name      = "/adp/${var.environment}/agent-factory/bootstrap"
    Module    = "webhook-ingress"
    Component = "hosted-agent-worker"
  })
}
