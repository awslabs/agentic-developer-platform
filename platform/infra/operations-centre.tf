# =============================================================================
# Operations Centre — platform/infra health dashboard (single inspection surface)
# =============================================================================
# The infra/health companion to the agent-observability dashboard (which owns
# the business/application lens). Together they realise the three-plane model
# (infra · app · business) without overlap. This is THE dashboard a human
# bookmarks to answer "is the platform OK?" in under 60 seconds.
#
# Three rows, one per health plane, each its own EPIC child story:
#   Row 1 — Platform health   (ContainerInsights)      #1709
#   Row 2 — Gateway health    (AWS/ApplicationELB)      #1710
#   Row 3 — Agent pipeline    (AWS/SQS)                 #1711
#
# Phase-1 rule: only AWS-managed namespaces (ContainerInsights, ApplicationELB,
# SQS) — they can't go stale and need no app-side instrumentation. App-EMF
# namespaces (ADP/Gateway, ADP/AgentTelemetry) are deferred until those
# emitters are confirmed steady; per-tenant/cost lives on agent-observability.
#
# Dynamic names: the gateway ALB and the agent SQS queues are created outside
# this stack (LB controller / agent-factory). Widgets therefore use CloudWatch
# SEARCH() expressions keyed on stable name prefixes — never a hardcoded ARN —
# so they survive resource recreation and auto-pick-up new queues.
#
# Naming:    adp-<env>-operations-centre   (infra/platform lens)
# Companion: adp-<env>-agent-observability (application/business lens)
#
# EPIC: #919   ·   Data source enabled by: PR #1704 (Container Insights)
# Pattern: modules/agent-factory/webhook-ingress/infra/agent-observability-dashboard.tf
# =============================================================================

resource "aws_cloudwatch_dashboard" "operations_centre" {
  dashboard_name = "${local.name_prefix}-operations-centre"

  dashboard_body = jsonencode({
    widgets = [
      # =======================================================================
      # Row 1 — PLATFORM HEALTH  (ContainerInsights)                     #1709
      # "Are the nodes and pods healthy?"
      # =======================================================================
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 6
        height = 6
        properties = {
          region  = var.aws_region
          title   = "Nodes (total vs failed)"
          view    = "timeSeries"
          stacked = false
          metrics = [
            ["ContainerInsights", "cluster_node_count", "ClusterName", "${local.name_prefix}-eks-cluster", { stat = "Average", label = "nodes" }],
            ["ContainerInsights", "cluster_failed_node_count", "ClusterName", "${local.name_prefix}-eks-cluster", { stat = "Maximum", label = "failed" }],
          ]
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "metric"
        x      = 6
        y      = 0
        width  = 6
        height = 6
        properties = {
          region = var.aws_region
          title  = "Running pods"
          view   = "timeSeries"
          metrics = [
            ["ContainerInsights", "cluster_number_of_running_pods", "ClusterName", "${local.name_prefix}-eks-cluster", { stat = "Average", label = "running pods" }],
          ]
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 6
        height = 6
        properties = {
          region = var.aws_region
          title  = "Node CPU / memory utilization (%)"
          view   = "timeSeries"
          metrics = [
            ["ContainerInsights", "node_cpu_utilization", "ClusterName", "${local.name_prefix}-eks-cluster", { stat = "Average", label = "cpu avg" }],
            ["...", { stat = "Maximum", label = "cpu max" }],
            ["ContainerInsights", "node_memory_utilization", "ClusterName", "${local.name_prefix}-eks-cluster", { stat = "Average", label = "mem avg" }],
            ["...", { stat = "Maximum", label = "mem max" }],
          ]
          yAxis = { left = { min = 0, max = 100 } }
        }
      },
      {
        type   = "metric"
        x      = 18
        y      = 0
        width  = 6
        height = 6
        properties = {
          region = var.aws_region
          title  = "Container restarts (crash signal)"
          view   = "timeSeries"
          metrics = [
            ["ContainerInsights", "pod_number_of_container_restarts", "ClusterName", "${local.name_prefix}-eks-cluster", { stat = "Sum", label = "restarts" }],
          ]
          yAxis = { left = { min = 0 } }
        }
      },

      # =======================================================================
      # Row 2 — GATEWAY HEALTH  (AWS/ApplicationELB, via SEARCH)         #1710
      # "Is the Bedrock gateway serving requests, and how fast?"
      # ALB name is dynamic (k8s-adpgatew-...) -> SEARCH on the prefix.
      # =======================================================================
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 8
        height = 6
        properties = {
          region = var.aws_region
          title  = "Gateway 5xx (ELB + target)"
          view   = "timeSeries"
          metrics = [
            [{ expression = "SEARCH('{AWS/ApplicationELB,LoadBalancer} MetricName=\"HTTPCode_Target_5XX_Count\" app/k8s-adpgatew', 'Sum', 300)", label = "target 5xx", id = "e1" }],
            [{ expression = "SEARCH('{AWS/ApplicationELB,LoadBalancer} MetricName=\"HTTPCode_ELB_5XX_Count\" app/k8s-adpgatew', 'Sum', 300)", label = "elb 5xx", id = "e2" }],
          ]
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 6
        width  = 8
        height = 6
        properties = {
          region = var.aws_region
          title  = "Gateway latency (target p95, s)"
          view   = "timeSeries"
          metrics = [
            [{ expression = "SEARCH('{AWS/ApplicationELB,LoadBalancer} MetricName=\"TargetResponseTime\" app/k8s-adpgatew', 'p95', 300)", label = "p95", id = "l1" }],
          ]
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "metric"
        x      = 16
        y      = 6
        width  = 8
        height = 6
        properties = {
          region = var.aws_region
          title  = "Gateway healthy hosts + request rate"
          view   = "timeSeries"
          metrics = [
            [{ expression = "SEARCH('{AWS/ApplicationELB,TargetGroup,LoadBalancer} MetricName=\"HealthyHostCount\" app/k8s-adpgatew', 'Average', 300)", label = "healthy hosts", id = "h1" }],
            [{ expression = "SEARCH('{AWS/ApplicationELB,TargetGroup,LoadBalancer} MetricName=\"UnHealthyHostCount\" app/k8s-adpgatew', 'Average', 300)", label = "unhealthy hosts", id = "h2" }],
            [{ expression = "SEARCH('{AWS/ApplicationELB,LoadBalancer} MetricName=\"RequestCount\" app/k8s-adpgatew', 'Sum', 300)", label = "requests", id = "h3", yAxis = "right" }],
          ]
          yAxis = { left = { min = 0 }, right = { min = 0 } }
        }
      },

      # =======================================================================
      # Row 3 — AGENT PIPELINE  (AWS/SQS, via SEARCH)                    #1711
      # "Is the agent delivery pipeline draining, or backing up / DLQ-ing?"
      # Queue names are dynamic (adp-<env>-agent-*) -> SEARCH on the prefix.
      # =======================================================================
      {
        type   = "metric"
        x      = 0
        y      = 12
        width  = 8
        height = 6
        properties = {
          region = var.aws_region
          title  = "Agent task/response queue depth (visible msgs)"
          view   = "timeSeries"
          metrics = [
            [{ expression = "SEARCH('{AWS/SQS,QueueName} MetricName=\"ApproximateNumberOfMessagesVisible\" adp-${var.environment}-agent NOT dlq', 'Maximum', 300)", label = "", id = "q1" }],
          ]
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 12
        width  = 8
        height = 6
        properties = {
          region = var.aws_region
          title  = "Agent DLQ depth (failed deliveries — should be 0)"
          view   = "timeSeries"
          metrics = [
            [{ expression = "SEARCH('{AWS/SQS,QueueName} MetricName=\"ApproximateNumberOfMessagesVisible\" adp-${var.environment}-agent dlq', 'Maximum', 300)", label = "", id = "d1" }],
          ]
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "metric"
        x      = 16
        y      = 12
        width  = 8
        height = 6
        properties = {
          region = var.aws_region
          title  = "Oldest message age (stuck-work signal, s)"
          view   = "timeSeries"
          metrics = [
            [{ expression = "SEARCH('{AWS/SQS,QueueName} MetricName=\"ApproximateAgeOfOldestMessage\" adp-${var.environment}-agent NOT dlq', 'Maximum', 300)", label = "", id = "a1" }],
          ]
          yAxis = { left = { min = 0 } }
        }
      },

      # -----------------------------------------------------------------------
      # Helper text
      # -----------------------------------------------------------------------
      {
        type   = "text"
        x      = 0
        y      = 18
        width  = 24
        height = 2
        properties = {
          markdown = "### adp-${var.environment}-operations-centre — platform health\n**Row 1** EKS nodes/pods (ContainerInsights) · **Row 2** gateway ALB 5xx/latency/hosts · **Row 3** agent SQS depth/DLQ. Business/cost metrics live on **adp-${var.environment}-agent-observability**. A non-zero DLQ or failed-node count is the first thing to investigate. EPIC #919."
        }
      },
    ]
  })
}

output "operations_centre_dashboard_url" {
  description = "Console URL for the operations-centre platform health dashboard"
  value       = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards/dashboard/${local.name_prefix}-operations-centre"
}
