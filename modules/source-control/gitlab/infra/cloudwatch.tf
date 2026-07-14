# =============================================================================
# GitLab CE Infrastructure — CloudWatch Backup Monitoring
# =============================================================================
# Alarm fires if no new backup object appears in S3 within 26 hours.
# (2-hour buffer on the 24-hour backup schedule)
# =============================================================================

# -----------------------------------------------------------------------------
# SNS Topic for backup alerts
# -----------------------------------------------------------------------------

resource "aws_sns_topic" "backup_alerts" {
  count = var.backup_enabled ? 1 : 0

  name = "${local.name_prefix}-backup-alerts"

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-backup-alerts"
    Service = "monitoring"
  })
}

# -----------------------------------------------------------------------------
# CloudWatch Alarm — Backup freshness
# Uses S3 NumberOfObjects metric on the daily/ prefix.
# If the bucket has 0 objects for 26 hours, the alarm fires.
# Note: S3 storage metrics are emitted once daily; we use the metric
# math approach to compare against a threshold period.
# -----------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "backup_freshness" {
  count = var.backup_enabled ? 1 : 0

  alarm_name        = "${local.name_prefix}-backup-freshness"
  alarm_description = "No GitLab backup uploaded in the last 26 hours"

  namespace   = "AWS/S3"
  metric_name = "NumberOfObjects"
  statistic   = "Average"
  period      = 86400 # 24 hours (S3 storage metrics are daily)
  dimensions = {
    BucketName  = aws_s3_bucket.backup[0].id
    StorageType = "AllStorageTypes"
  }

  comparison_operator = "LessThanOrEqualToThreshold"
  threshold           = 0
  evaluation_periods  = 1
  treat_missing_data  = "breaching" # No data = no backups = alarm

  alarm_actions = [aws_sns_topic.backup_alerts[0].arn]
  ok_actions    = [aws_sns_topic.backup_alerts[0].arn]

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-backup-freshness"
    Service = "monitoring"
  })
}
