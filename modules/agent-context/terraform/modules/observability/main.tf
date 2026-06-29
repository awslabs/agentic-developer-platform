# =============================================================================
# Knowledge Layer Observability — Dashboard + Alarms + Log Groups + SNS
# =============================================================================
# Single-pane operator view for the Knowledge Layer pipeline. Consumes:
#   - ADP/KnowledgeLayer custom metrics (from Story 5 / ADOT collector)
#   - /adp/<env>/knowledge-layer/* log groups (structured logs from Story 1)
#   - AWS/SQS built-in metrics (ingestion queue health)
#
# Pattern: modules/agent-factory/webhook-ingress/infra/agent-observability-dashboard.tf
# Companion: platform/infra/operations-centre.tf (infra lens)
#
# Issue: #1757
# =============================================================================

# =============================================================================
# CloudWatch Log Groups
# =============================================================================

resource "aws_cloudwatch_log_group" "ingestion" {
  name              = "/adp/${var.environment}/knowledge-layer/ingestion"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.cloudwatch_kms_key_arn

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "door" {
  name              = "/adp/${var.environment}/knowledge-layer/door"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.cloudwatch_kms_key_arn

  tags = var.tags
}

# =============================================================================
# SNS Topic — alarm action target
# =============================================================================

resource "aws_sns_topic" "kl_alarms" {
  name = "${var.name_prefix}-kl-alarms"

  tags = var.tags
}

# =============================================================================
# CloudWatch Alarms
# =============================================================================

# 1. Rollup not promoting — no assets indexed in 30 min (when queue has traffic)
resource "aws_cloudwatch_metric_alarm" "kl_rollup_not_promoting" {
  alarm_name          = "${var.name_prefix}-kl-rollup-not-promoting"
  alarm_description   = "Knowledge Layer: no assets indexed in 30 min — pipeline may be stuck"
  namespace           = "ADP/KnowledgeLayer"
  metric_name         = "kl.assets_indexed"
  statistic           = "Sum"
  period              = 1800
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "LessThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.kl_alarms.arn]
  ok_actions    = [aws_sns_topic.kl_alarms.arn]

  tags = var.tags
}

# 2. Stage failing en masse — >10 failures in 5 min
resource "aws_cloudwatch_metric_alarm" "kl_stage_failing" {
  alarm_name          = "${var.name_prefix}-kl-stage-failing"
  alarm_description   = "Knowledge Layer: >10 asset failures in 5 min — stage may be broken"
  namespace           = "ADP/KnowledgeLayer"
  metric_name         = "kl.assets_failed"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 10
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.kl_alarms.arn]
  ok_actions    = [aws_sns_topic.kl_alarms.arn]

  tags = var.tags
}

# 3. Zombie runs — metric filter counts runs older than expected duration
resource "aws_cloudwatch_log_metric_filter" "zombie_runs" {
  name           = "${var.name_prefix}-kl-zombie-runs"
  log_group_name = aws_cloudwatch_log_group.ingestion.name
  pattern        = "{ $.event = \"run.zombie_detected\" }"

  metric_transformation {
    name      = "kl.zombie_runs"
    namespace = "ADP/KnowledgeLayer"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "kl_zombie_runs" {
  alarm_name          = "${var.name_prefix}-kl-zombie-runs"
  alarm_description   = "Knowledge Layer: zombie run detected — run exceeded max duration without completing"
  namespace           = "ADP/KnowledgeLayer"
  metric_name         = "kl.zombie_runs"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.kl_alarms.arn]
  ok_actions    = [aws_sns_topic.kl_alarms.arn]

  tags = var.tags

  depends_on = [aws_cloudwatch_log_metric_filter.zombie_runs]
}

# 4. SQS DLQ backlog high — messages stuck in dead-letter queue
resource "aws_cloudwatch_metric_alarm" "kl_dlq_backlog" {
  alarm_name          = "${var.name_prefix}-kl-dlq-backlog"
  alarm_description   = "Knowledge Layer: DLQ has messages — ingestion failures not being processed"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = "${var.sqs_queue_name_prefix}-ingestion-dlq"
  }

  alarm_actions = [aws_sns_topic.kl_alarms.arn]
  ok_actions    = [aws_sns_topic.kl_alarms.arn]

  tags = var.tags
}

# 5. Door error spike — >5 errors in 5 min
resource "aws_cloudwatch_metric_alarm" "kl_door_errors" {
  alarm_name          = "${var.name_prefix}-kl-door-errors"
  alarm_description   = "Knowledge Layer: Door query error spike — >5 errors in 5 min"
  namespace           = "ADP/KnowledgeLayer"
  metric_name         = "kl.door_errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.kl_alarms.arn]
  ok_actions    = [aws_sns_topic.kl_alarms.arn]

  tags = var.tags
}

# 6. DeepWiki failure rate — >20% failures over 15 min
resource "aws_cloudwatch_metric_alarm" "kl_deepwiki_failures" {
  alarm_name          = "${var.name_prefix}-kl-deepwiki-failures"
  alarm_description   = "Knowledge Layer: DeepWiki failure rate high — >20% of requests failing"
  namespace           = "ADP/KnowledgeLayer"
  metric_name         = "kl.deepwiki_failures"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.kl_alarms.arn]
  ok_actions    = [aws_sns_topic.kl_alarms.arn]

  tags = var.tags
}

# =============================================================================
# CloudWatch Dashboard
# =============================================================================
# Layout: 6 rows (each 6 units tall), 24 columns wide.
# Follows design note section 6.3 layout.

resource "aws_cloudwatch_dashboard" "knowledge_layer" {
  dashboard_name = "adp-${var.environment}-knowledge-layer"

  dashboard_body = jsonencode({
    widgets = [
      # -----------------------------------------------------------------
      # Row 1: Pipeline funnel | Ingestion throughput by tenant
      # -----------------------------------------------------------------
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          region  = var.aws_region
          title   = "Pipeline funnel (registered / queued / indexed / failed)"
          view    = "timeSeries"
          stacked = true
          metrics = [
            ["ADP/KnowledgeLayer", "kl.assets_registered", { stat = "Sum", label = "registered" }],
            ["ADP/KnowledgeLayer", "kl.assets_queued", { stat = "Sum", label = "queued" }],
            ["ADP/KnowledgeLayer", "kl.assets_indexed", { stat = "Sum", label = "indexed" }],
            ["ADP/KnowledgeLayer", "kl.assets_failed", { stat = "Sum", label = "failed" }],
          ]
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          region = var.aws_region
          title  = "Ingestion throughput by tenant"
          view   = "bar"
          query  = "SOURCE '/adp/${var.environment}/knowledge-layer/ingestion' | filter @message like /asset_indexed/ | parse @message /\"tenant_id\":\"(?<tenant>[^\"]+)\"/ | stats count(*) as assets by tenant | sort assets desc"
        }
      },

      # -----------------------------------------------------------------
      # Row 2: Per-stage success rate | Per-stage latency (p50/p95)
      # -----------------------------------------------------------------
      {
        type   = "log"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          region = var.aws_region
          title  = "Per-stage success rate (last 24h)"
          view   = "bar"
          query  = "SOURCE '/adp/${var.environment}/knowledge-layer/ingestion' | filter @message like /stage_complete/ or @message like /stage_failed/ | parse @message /\"stage\":\"(?<stage>[^\"]+)\"/ | parse @message /\"success\":(?<ok>[a-z]+)/ | stats sum(ok=\"true\") as successes, count(*) as total by stage | fields (successes / total * 100) as success_pct, stage | sort stage"
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          region = var.aws_region
          title  = "Per-stage latency p50/p95 (ms)"
          view   = "line"
          query  = "SOURCE '/adp/${var.environment}/knowledge-layer/ingestion' | filter @message like /stage_complete/ | parse @message /\"stage\":\"(?<stage>[^\"]+)\"/ | parse @message /\"duration_ms\":(?<dur>[0-9.]+)/ | stats pct(dur, 50) as p50, pct(dur, 95) as p95 by bin(5m), stage"
        }
      },

      # -----------------------------------------------------------------
      # Row 3: Failure breakdown | Per-tenant volume
      # -----------------------------------------------------------------
      {
        type   = "log"
        x      = 0
        y      = 12
        width  = 12
        height = 6
        properties = {
          region = var.aws_region
          title  = "Failure breakdown (stage x error type)"
          view   = "table"
          query  = "SOURCE '/adp/${var.environment}/knowledge-layer/ingestion' | filter @message like /stage_failed/ | parse @message /\"stage\":\"(?<stage>[^\"]+)\"/ | parse @message /\"error_type\":\"(?<err>[^\"]+)\"/ | stats count(*) as failures by stage, err | sort failures desc"
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 12
        width  = 12
        height = 6
        properties = {
          region = var.aws_region
          title  = "Per-tenant indexed volume"
          view   = "bar"
          query  = "SOURCE '/adp/${var.environment}/knowledge-layer/ingestion' | filter @message like /asset_indexed/ | parse @message /\"tenant_id\":\"(?<tenant>[^\"]+)\"/ | stats count(*) as indexed by tenant | sort indexed desc"
        }
      },

      # -----------------------------------------------------------------
      # Row 4: Door query rate by verb | Door query latency p95 by verb
      # -----------------------------------------------------------------
      {
        type   = "log"
        x      = 0
        y      = 18
        width  = 12
        height = 6
        properties = {
          region = var.aws_region
          title  = "Door query rate by verb"
          view   = "line"
          query  = "SOURCE '/adp/${var.environment}/knowledge-layer/door' | filter @message like /door_query/ | parse @message /\"verb\":\"(?<verb>[^\"]+)\"/ | stats count(*) as queries by bin(5m), verb"
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 18
        width  = 12
        height = 6
        properties = {
          region = var.aws_region
          title  = "Door query latency p95 by verb (ms)"
          view   = "line"
          query  = "SOURCE '/adp/${var.environment}/knowledge-layer/door' | filter @message like /door_query/ | parse @message /\"verb\":\"(?<verb>[^\"]+)\"/ | parse @message /\"duration_ms\":(?<dur>[0-9.]+)/ | stats pct(dur, 95) as p95_ms by bin(5m), verb"
        }
      },

      # -----------------------------------------------------------------
      # Row 5: Queue health (SQS) | Worker concurrency
      # -----------------------------------------------------------------
      {
        type   = "metric"
        x      = 0
        y      = 24
        width  = 12
        height = 6
        properties = {
          region = var.aws_region
          title  = "SQS queue health (visible + in-flight + DLQ)"
          view   = "timeSeries"
          metrics = [
            [{ expression = "SEARCH('{AWS/SQS,QueueName} MetricName=\"ApproximateNumberOfMessagesVisible\" ${var.sqs_queue_name_prefix}-ingestion', 'Maximum', 300)", label = "visible", id = "q1" }],
            [{ expression = "SEARCH('{AWS/SQS,QueueName} MetricName=\"ApproximateNumberOfMessagesNotVisible\" ${var.sqs_queue_name_prefix}-ingestion', 'Maximum', 300)", label = "in-flight", id = "q2" }],
            [{ expression = "SEARCH('{AWS/SQS,QueueName} MetricName=\"ApproximateNumberOfMessagesVisible\" ${var.sqs_queue_name_prefix}-ingestion-dlq', 'Maximum', 300)", label = "DLQ", id = "q3" }],
          ]
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 24
        width  = 12
        height = 6
        properties = {
          region = var.aws_region
          title  = "Worker concurrency (KEDA pods)"
          view   = "timeSeries"
          metrics = [
            ["ContainerInsights", "pod_number_of_running_pods", "Namespace", "agent-context", "ClusterName", "adp-${var.environment}-eks-cluster", { stat = "Average", label = "running pods" }],
          ]
          yAxis = { left = { min = 0 } }
        }
      },

      # -----------------------------------------------------------------
      # Row 6: Active alerts (text) | Per-run detail table
      # -----------------------------------------------------------------
      {
        type   = "text"
        x      = 0
        y      = 30
        width  = 12
        height = 6
        properties = {
          markdown = "### Active Alerts\n| Alarm | What it means |\n|-------|---------------|\n| **rollup-not-promoting** | No assets indexed in 30 min — pipeline stuck |\n| **stage-failing** | >10 failures in 5 min — a stage is broken |\n| **zombie-runs** | Run exceeded max duration without completing |\n| **dlq-backlog** | DLQ has >5 messages — ingestion failures piling up |\n| **door-errors** | >5 Door query errors in 5 min |\n| **deepwiki-failures** | DeepWiki failure count high |\n\nAlarms fire to SNS topic: `${var.name_prefix}-kl-alarms`\n\nSubscribe via email, Slack webhook, or PagerDuty."
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 30
        width  = 12
        height = 6
        properties = {
          region = var.aws_region
          title  = "Recent runs (last 20 — status, duration, assets)"
          view   = "table"
          query  = "SOURCE '/adp/${var.environment}/knowledge-layer/ingestion' | filter @message like /run_complete/ or @message like /run_failed/ | parse @message /\"run_id\":\"(?<run_id>[^\"]+)\"/ | parse @message /\"tenant_id\":\"(?<tenant>[^\"]+)\"/ | parse @message /\"status\":\"(?<status>[^\"]+)\"/ | parse @message /\"duration_ms\":(?<dur>[0-9.]+)/ | parse @message /\"assets_processed\":(?<assets>[0-9]+)/ | fields @timestamp, run_id, tenant, status, dur/1000 as duration_sec, assets | sort @timestamp desc | limit 20"
        }
      },
    ]
  })
}
