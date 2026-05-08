# =============================================================================
# Issue #144: Unified End-to-End Latency Dashboard
# =============================================================================
# CloudFront → ALB → Gateway Pod → Bedrock
#
# Data sources:
# - CloudFront: OriginLatency (additional metrics), Requests, Error Rates
# - ALB: TargetResponseTime, RequestCount, HTTP status codes
# - Gateway Pod: Logs Insights queries against Container Insights app logs
# - X-Ray: ApproximateTraceCount
# - Container Insights: Pod CPU, Memory, Network
#
# NOTE: ALB metrics require alb_arn_suffix which is only known after the
# EKS Ingress controller creates the ALB. If empty, ALB widgets show no data.

locals {
  ci_log_group = "/aws/containerinsights/${var.eks_cluster_name}/application"
}

resource "aws_cloudwatch_dashboard" "latency" {
  dashboard_name = "${var.name_prefix}-latency"

  dashboard_body = jsonencode({
    widgets = concat(
      # =====================================================================
      # Header
      # =====================================================================
      [
        {
          type   = "text"
          x      = 0
          y      = 0
          width  = 24
          height = 1
          properties = {
            markdown = "# Bedrock Gateway — End-to-End Latency Dashboard (${var.environment})\nCloudFront → ALB → Gateway Pod → Bedrock"
          }
        },
      ],

      # =====================================================================
      # CloudFront Section
      # =====================================================================
      [
        {
          type   = "text"
          x      = 0
          y      = 1
          width  = 24
          height = 1
          properties = {
            markdown = "## 🌐 CloudFront (Edge → Origin)"
          }
        },
        {
          type   = "metric"
          x      = 0
          y      = 2
          width  = 8
          height = 6
          properties = {
            title = "CloudFront Origin Latency (p50 / p90 / p99)"
            metrics = [
              ["AWS/CloudFront", "OriginLatency", "DistributionId", var.cloudfront_distribution_id, "Region", "Global", { stat = "p50", label = "p50" }],
              ["...", { stat = "p90", label = "p90" }],
              ["...", { stat = "p99", label = "p99" }],
            ]
            view    = "timeSeries"
            stacked = false
            region  = "us-east-1"
            period  = 60
            yAxis   = { left = { label = "ms", showUnits = false } }
          }
        },
        {
          type   = "metric"
          x      = 8
          y      = 2
          width  = 8
          height = 6
          properties = {
            title = "CloudFront Requests / min"
            metrics = [
              ["AWS/CloudFront", "Requests", "DistributionId", var.cloudfront_distribution_id, "Region", "Global", { stat = "Sum", label = "Requests" }],
            ]
            view    = "timeSeries"
            stacked = false
            region  = "us-east-1"
            period  = 60
            yAxis   = { left = { label = "count", showUnits = false } }
          }
        },
        {
          type   = "metric"
          x      = 16
          y      = 2
          width  = 8
          height = 6
          properties = {
            title = "CloudFront Error Rates"
            metrics = [
              ["AWS/CloudFront", "4xxErrorRate", "DistributionId", var.cloudfront_distribution_id, "Region", "Global", { stat = "Average", label = "4xx %" }],
              ["AWS/CloudFront", "5xxErrorRate", "DistributionId", var.cloudfront_distribution_id, "Region", "Global", { stat = "Average", label = "5xx %" }],
            ]
            view    = "timeSeries"
            stacked = false
            region  = "us-east-1"
            period  = 60
            yAxis   = { left = { label = "%", showUnits = false } }
          }
        },
      ],

      # =====================================================================
      # ALB Section
      # ALB metrics require a non-empty alb_arn_suffix. When the ALB has not
      # yet been created (suffix is empty), we omit these widgets entirely to
      # avoid CloudWatch API validation errors on empty dimension values.
      # =====================================================================
      var.alb_arn_suffix != "" ? [
        {
          type   = "text"
          x      = 0
          y      = 8
          width  = 24
          height = 1
          properties = {
            markdown = "## ⚖️ ALB (Load Balancer → Pod)"
          }
        },
        {
          type   = "metric"
          x      = 0
          y      = 9
          width  = 8
          height = 6
          properties = {
            title = "ALB Target Response Time (p50 / p90 / p99)"
            metrics = [
              ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", var.alb_arn_suffix, { stat = "p50", label = "p50" }],
              ["...", { stat = "p90", label = "p90" }],
              ["...", { stat = "p99", label = "p99" }],
            ]
            view    = "timeSeries"
            stacked = false
            region  = var.aws_region
            period  = 60
            yAxis   = { left = { label = "seconds", showUnits = false } }
          }
        },
        {
          type   = "metric"
          x      = 8
          y      = 9
          width  = 8
          height = 6
          properties = {
            title = "ALB Request Count / min"
            metrics = [
              ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", var.alb_arn_suffix, { stat = "Sum", label = "Requests" }],
            ]
            view    = "timeSeries"
            stacked = false
            region  = var.aws_region
            period  = 60
            yAxis   = { left = { label = "count", showUnits = false } }
          }
        },
        {
          type   = "metric"
          x      = 16
          y      = 9
          width  = 8
          height = 6
          properties = {
            title = "ALB HTTP Status Codes"
            metrics = [
              ["AWS/ApplicationELB", "HTTPCode_Target_2XX_Count", "LoadBalancer", var.alb_arn_suffix, { stat = "Sum", label = "2xx", color = "#2ca02c" }],
              ["AWS/ApplicationELB", "HTTPCode_Target_4XX_Count", "LoadBalancer", var.alb_arn_suffix, { stat = "Sum", label = "4xx", color = "#ff7f0e" }],
              ["AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "LoadBalancer", var.alb_arn_suffix, { stat = "Sum", label = "5xx", color = "#d62728" }],
              ["AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", "LoadBalancer", var.alb_arn_suffix, { stat = "Sum", label = "ALB 5xx", color = "#9467bd" }],
            ]
            view    = "timeSeries"
            stacked = false
            region  = var.aws_region
            period  = 60
            yAxis   = { left = { label = "count", showUnits = false } }
          }
        },
      ] : [],

      # =====================================================================
      # Gateway Pod Section (Logs Insights)
      # =====================================================================
      [
        {
          type   = "text"
          x      = 0
          y      = 15
          width  = 24
          height = 1
          properties = {
            markdown = "## 🏗️ Gateway Pod (Application-Level Latency from Logs)"
          }
        },

        # --- KEY WIDGET: Bedrock Time vs Gateway Overhead (stacked) ---
        {
          type   = "log"
          x      = 0
          y      = 16
          width  = 12
          height = 6
          properties = {
            title   = "⏱️ Bedrock Time vs Gateway Overhead (avg ms)"
            query   = "SOURCE '${local.ci_log_group}' | fields @timestamp, @message\n| parse @message '\"event\": \"*\"' as event\n| parse @message '\"path\": \"*\"' as path\n| parse @message '\"timings\": {*}' as timings_raw\n| filter event = 'request_end' and path like '/model/' and timings_raw like '\"bedrock\"'\n| parse timings_raw '\"bedrock\": *,' as bedrock_ms\n| parse timings_raw '\"total\": *}' as total_ms\n| fields (total_ms - bedrock_ms) as gateway_overhead_ms, bedrock_ms\n| stats avg(bedrock_ms) as `Bedrock (ms)`, avg(gateway_overhead_ms) as `Gateway Overhead (ms)` by bin(5m)"
            region  = var.aws_region
            stacked = true
            view    = "timeSeries"
          }
        },

        # --- KEY WIDGET: % of time spent in Bedrock ---
        {
          type   = "log"
          x      = 12
          y      = 16
          width  = 12
          height = 6
          properties = {
            title   = "📊 Bedrock % of Total Request Time"
            query   = "SOURCE '${local.ci_log_group}' | fields @timestamp, @message\n| parse @message '\"event\": \"*\"' as event\n| parse @message '\"path\": \"*\"' as path\n| parse @message '\"timings\": {*}' as timings_raw\n| filter event = 'request_end' and path like '/model/' and timings_raw like '\"bedrock\"'\n| parse timings_raw '\"bedrock\": *,' as bedrock_ms\n| parse timings_raw '\"total\": *}' as total_ms\n| fields (bedrock_ms / total_ms) * 100 as bedrock_pct\n| stats avg(bedrock_pct) as `Bedrock %`, min(bedrock_pct) as `Min %`, max(bedrock_pct) as `Max %`, count() as requests by bin(5m)"
            region  = var.aws_region
            stacked = false
            view    = "timeSeries"
          }
        },

        # --- Bedrock latency by model (using bedrock_ms from timings) ---
        {
          type   = "log"
          x      = 0
          y      = 22
          width  = 12
          height = 6
          properties = {
            title   = "Bedrock Latency by Model (from timings.bedrock)"
            query   = "SOURCE '${local.ci_log_group}' | fields @timestamp, @message\n| parse @message '\"path\": \"*\"' as path\n| parse @message '\"event\": \"*\"' as event\n| parse @message '\"timings\": {*}' as timings_raw\n| filter event = 'request_end' and path like '/model/' and timings_raw like '\"bedrock\"'\n| parse timings_raw '\"bedrock\": *,' as bedrock_ms\n| parse timings_raw '\"total\": *}' as total_ms\n| parse path '/model/*/invoke' as model_path\n| fields replace(model_path, 'global.anthropic.', '') as model\n| fields replace(model, 'us.anthropic.', '') as model\n| fields (total_ms - bedrock_ms) as overhead_ms\n| stats avg(bedrock_ms) as avg_bedrock, pct(bedrock_ms, 50) as p50_bedrock, pct(bedrock_ms, 90) as p90_bedrock, max(bedrock_ms) as max_bedrock, avg(overhead_ms) as avg_overhead, count() as reqs by model\n| sort avg_bedrock desc"
            region  = var.aws_region
            stacked = false
            view    = "table"
          }
        },

        # --- Slow request buckets (how many >30s, >60s, >120s) ---
        {
          type   = "log"
          x      = 12
          y      = 22
          width  = 12
          height = 6
          properties = {
            title   = "🐌 Slow Bedrock Requests (>10s / >30s / >60s / >120s)"
            query   = "SOURCE '${local.ci_log_group}' | fields @timestamp, @message\n| parse @message '\"event\": \"*\"' as event\n| parse @message '\"path\": \"*\"' as path\n| parse @message '\"timings\": {*}' as timings_raw\n| filter event = 'request_end' and path like '/model/' and timings_raw like '\"bedrock\"'\n| parse timings_raw '\"bedrock\": *,' as bedrock_ms\n| stats count() as total, sum(case(bedrock_ms > 10000, 1, 0)) as `>10s`, sum(case(bedrock_ms > 30000, 1, 0)) as `>30s`, sum(case(bedrock_ms > 60000, 1, 0)) as `>60s`, sum(case(bedrock_ms > 120000, 1, 0)) as `>120s` by bin(5m)"
            region  = var.aws_region
            stacked = false
            view    = "timeSeries"
          }
        },

        # --- Gateway component breakdown ---
        {
          type   = "log"
          x      = 0
          y      = 28
          width  = 12
          height = 6
          properties = {
            title   = "Gateway Component Breakdown (from timings)"
            query   = "SOURCE '${local.ci_log_group}' | fields @timestamp, @message\n| parse @message '\"event\": \"*\"' as event\n| parse @message '\"path\": \"*\"' as path\n| parse @message '\"timings\": {*}' as timings_raw\n| filter event = 'request_end' and path like '/model/'\n| parse timings_raw '\"auth\": *,' as auth_ms\n| parse timings_raw '\"budget_check\": *,' as budget_ms\n| parse timings_raw '\"ratelimit_check\": *,' as ratelimit_ms\n| parse timings_raw '\"bedrock\": *,' as bedrock_ms\n| parse timings_raw '\"total\": *}' as total_ms\n| stats avg(auth_ms) as avg_auth, avg(budget_ms) as avg_budget, avg(ratelimit_ms) as avg_ratelimit, avg(bedrock_ms) as avg_bedrock, avg(total_ms) as avg_total, count() as requests by bin(5m)"
            region  = var.aws_region
            stacked = false
            view    = "timeSeries"
          }
        },

        # --- Streaming vs Non-Streaming ---
        {
          type   = "log"
          x      = 12
          y      = 28
          width  = 12
          height = 6
          properties = {
            title   = "Streaming vs Non-Streaming Latency"
            query   = "SOURCE '${local.ci_log_group}' | fields @timestamp, @message\n| parse @message '\"latency_ms\": *,' as latency_ms\n| parse @message '\"event\": \"*\"' as event\n| parse @message '\"path\": \"*\"' as path\n| filter event = 'request_end' and path like '/model/'\n| fields case(path like 'invoke-with-response-stream', 'streaming', 1, 'non-streaming') as req_type\n| stats avg(latency_ms) as avg_ms, pct(latency_ms, 50) as p50_ms, pct(latency_ms, 95) as p95_ms, count() as requests by req_type, bin(5m)"
            region  = var.aws_region
            stacked = false
            view    = "timeSeries"
          }
        },
      ],

      # =====================================================================
      # X-Ray Traces Section
      # =====================================================================
      [
        {
          type   = "text"
          x      = 0
          y      = 34
          width  = 24
          height = 1
          properties = {
            markdown = "## 📊 X-Ray Traces"
          }
        },
        {
          type   = "metric"
          x      = 0
          y      = 35
          width  = 8
          height = 6
          properties = {
            title = "X-Ray Trace Count"
            metrics = [
              ["AWS/X-Ray", "ApproximateTraceCount", "GroupName", "Default", { stat = "Sum", label = "Traces" }],
            ]
            view    = "timeSeries"
            stacked = false
            region  = var.aws_region
            period  = 60
          }
        },
        {
          type   = "log"
          x      = 8
          y      = 35
          width  = 16
          height = 6
          properties = {
            title   = "Slowest Requests (last 1h)"
            query   = "SOURCE '${local.ci_log_group}' | fields @timestamp, @message\n| parse @message '\"latency_ms\": *,' as latency_ms\n| parse @message '\"event\": \"*\"' as event\n| parse @message '\"path\": \"*\"' as path\n| parse @message '\"status_code\": *,' as status\n| parse @message '\"timings\": {*}' as timings_raw\n| filter event = 'request_end' and path like '/model/'\n| parse timings_raw '\"bedrock\": *,' as bedrock_ms\n| fields (latency_ms - bedrock_ms) as overhead_ms\n| sort latency_ms desc\n| limit 10\n| fields @timestamp, latency_ms, bedrock_ms, overhead_ms, status, path"
            region  = var.aws_region
            stacked = false
            view    = "table"
          }
        },
      ],

      # =====================================================================
      # Pod Health Section (Container Insights)
      # =====================================================================
      [
        {
          type   = "text"
          x      = 0
          y      = 41
          width  = 24
          height = 1
          properties = {
            markdown = "## 🖥️ Pod Health (Container Insights)"
          }
        },
        {
          type   = "metric"
          x      = 0
          y      = 42
          width  = 8
          height = 6
          properties = {
            title = "Pod CPU Utilization"
            metrics = [
              ["ContainerInsights", "pod_cpu_utilization", "PodName", var.pod_deployment_name, "ClusterName", var.eks_cluster_name, "Namespace", var.eks_namespace, { stat = "Average", label = "CPU %" }],
            ]
            view    = "timeSeries"
            stacked = false
            region  = var.aws_region
            period  = 60
            yAxis   = { left = { label = "%", showUnits = false } }
          }
        },
        {
          type   = "metric"
          x      = 8
          y      = 42
          width  = 8
          height = 6
          properties = {
            title = "Pod Memory Utilization"
            metrics = [
              ["ContainerInsights", "pod_memory_utilization", "PodName", var.pod_deployment_name, "ClusterName", var.eks_cluster_name, "Namespace", var.eks_namespace, { stat = "Average", label = "Memory %" }],
            ]
            view    = "timeSeries"
            stacked = false
            region  = var.aws_region
            period  = 60
            yAxis   = { left = { label = "%", showUnits = false } }
          }
        },
        {
          type   = "metric"
          x      = 16
          y      = 42
          width  = 8
          height = 6
          properties = {
            title = "Pod Network (Rx/Tx bytes)"
            metrics = [
              ["ContainerInsights", "pod_network_rx_bytes", "PodName", var.pod_deployment_name, "ClusterName", var.eks_cluster_name, "Namespace", var.eks_namespace, { stat = "Average", label = "Rx bytes/s" }],
              ["ContainerInsights", "pod_network_tx_bytes", "PodName", var.pod_deployment_name, "ClusterName", var.eks_cluster_name, "Namespace", var.eks_namespace, { stat = "Average", label = "Tx bytes/s" }],
            ]
            view    = "timeSeries"
            stacked = false
            region  = var.aws_region
            period  = 60
          }
        },
      ],
    )
  })
}
