#!/usr/bin/env python3
"""Generate and deploy the updated CloudWatch latency dashboard."""
import json
import subprocess
import sys

LOG_GROUP = "/aws/containerinsights/bedrockgw-dev-eks-cluster/application"
CF_DIST = "E2YPAJAB9V68EN"
ALB = "app/k8s-bedrockg-bedrockg-96a0136fc5/a04d4e1ab78a9b6c"
REGION = "us-east-1"
DASH_NAME = "bedrockgw-dev-latency"

widgets = [
    # Header
    {"type": "text", "x": 0, "y": 0, "width": 24, "height": 1,
     "properties": {"markdown": "# Bedrock Gateway — End-to-End Latency Dashboard (dev)\nCloudFront → ALB → Gateway Pod → Bedrock"}},

    # CloudFront
    {"type": "text", "x": 0, "y": 1, "width": 24, "height": 1,
     "properties": {"markdown": "## 🌐 CloudFront (Edge → Origin)"}},
    {"type": "metric", "x": 0, "y": 2, "width": 8, "height": 6,
     "properties": {"title": "CloudFront Origin Latency (p50/p90/p99)",
      "metrics": [
        ["AWS/CloudFront", "OriginLatency", "DistributionId", CF_DIST, "Region", "Global", {"stat": "p50", "label": "p50"}],
        ["...", {"stat": "p90", "label": "p90"}],
        ["...", {"stat": "p99", "label": "p99"}]],
      "view": "timeSeries", "stacked": False, "region": "us-east-1", "period": 60,
      "yAxis": {"left": {"label": "ms", "showUnits": False}}}},
    {"type": "metric", "x": 8, "y": 2, "width": 8, "height": 6,
     "properties": {"title": "CloudFront Requests/min",
      "metrics": [["AWS/CloudFront", "Requests", "DistributionId", CF_DIST, "Region", "Global", {"stat": "Sum", "label": "Requests"}]],
      "view": "timeSeries", "stacked": False, "region": "us-east-1", "period": 60}},
    {"type": "metric", "x": 16, "y": 2, "width": 8, "height": 6,
     "properties": {"title": "CloudFront Error Rates",
      "metrics": [
        ["AWS/CloudFront", "4xxErrorRate", "DistributionId", CF_DIST, "Region", "Global", {"stat": "Average", "label": "4xx %"}],
        ["AWS/CloudFront", "5xxErrorRate", "DistributionId", CF_DIST, "Region", "Global", {"stat": "Average", "label": "5xx %"}]],
      "view": "timeSeries", "stacked": False, "region": "us-east-1", "period": 60}},

    # ALB
    {"type": "text", "x": 0, "y": 8, "width": 24, "height": 1,
     "properties": {"markdown": "## ⚖️ ALB (Load Balancer → Pod)"}},
    {"type": "metric", "x": 0, "y": 9, "width": 8, "height": 6,
     "properties": {"title": "ALB Target Response Time (p50/p90/p99)",
      "metrics": [
        ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", ALB, {"stat": "p50", "label": "p50"}],
        ["...", {"stat": "p90", "label": "p90"}],
        ["...", {"stat": "p99", "label": "p99"}]],
      "view": "timeSeries", "stacked": False, "region": REGION, "period": 60}},
    {"type": "metric", "x": 8, "y": 9, "width": 8, "height": 6,
     "properties": {"title": "ALB Request Count/min",
      "metrics": [["AWS/ApplicationELB", "RequestCount", "LoadBalancer", ALB, {"stat": "Sum", "label": "Requests"}]],
      "view": "timeSeries", "stacked": False, "region": REGION, "period": 60}},
    {"type": "metric", "x": 16, "y": 9, "width": 8, "height": 6,
     "properties": {"title": "ALB HTTP Status Codes",
      "metrics": [
        ["AWS/ApplicationELB", "HTTPCode_Target_2XX_Count", "LoadBalancer", ALB, {"stat": "Sum", "label": "2xx", "color": "#2ca02c"}],
        ["AWS/ApplicationELB", "HTTPCode_Target_4XX_Count", "LoadBalancer", ALB, {"stat": "Sum", "label": "4xx", "color": "#ff7f0e"}],
        ["AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "LoadBalancer", ALB, {"stat": "Sum", "label": "5xx", "color": "#d62728"}]],
      "view": "timeSeries", "stacked": False, "region": REGION, "period": 60}},

    # Gateway Pod section
    {"type": "text", "x": 0, "y": 15, "width": 24, "height": 1,
     "properties": {"markdown": "## 🏗️ Gateway Pod (Application-Level Latency from Logs)"}},

    # KEY WIDGET: Bedrock Time vs Gateway Overhead (stacked area)
    {"type": "log", "x": 0, "y": 16, "width": 12, "height": 6,
     "properties": {
      "title": "⏱️ Bedrock Time vs Gateway Overhead (avg ms)",
      "query": f"SOURCE '{LOG_GROUP}' | fields @timestamp, @message\n"
               "| parse @message '\"event\": \"*\"' as event\n"
               "| parse @message '\"path\": \"*\"' as path\n"
               "| parse @message '\"timings\": {*}' as timings_raw\n"
               "| filter event = 'request_end' and path like '/model/' and timings_raw like '\"bedrock\"'\n"
               "| parse timings_raw '\"bedrock\": *,' as bedrock_ms\n"
               "| parse timings_raw '\"total\": *}' as total_ms\n"
               "| fields (total_ms - bedrock_ms) as gateway_overhead_ms, bedrock_ms\n"
               "| stats avg(bedrock_ms) as `Bedrock (ms)`, avg(gateway_overhead_ms) as `Gateway Overhead (ms)` by bin(5m)",
      "region": REGION, "stacked": True, "view": "timeSeries"}},

    # KEY WIDGET: Bedrock % of total request time
    {"type": "log", "x": 12, "y": 16, "width": 12, "height": 6,
     "properties": {
      "title": "📊 Bedrock % of Total Request Time",
      "query": f"SOURCE '{LOG_GROUP}' | fields @timestamp, @message\n"
               "| parse @message '\"event\": \"*\"' as event\n"
               "| parse @message '\"path\": \"*\"' as path\n"
               "| parse @message '\"timings\": {*}' as timings_raw\n"
               "| filter event = 'request_end' and path like '/model/' and timings_raw like '\"bedrock\"'\n"
               "| parse timings_raw '\"bedrock\": *,' as bedrock_ms\n"
               "| parse timings_raw '\"total\": *}' as total_ms\n"
               "| fields (bedrock_ms / total_ms) * 100 as bedrock_pct\n"
               "| stats avg(bedrock_pct) as `Bedrock %`, min(bedrock_pct) as `Min %`, max(bedrock_pct) as `Max %`, count() as requests by bin(5m)",
      "region": REGION, "stacked": False, "view": "timeSeries"}},

    # Bedrock latency by model table
    {"type": "log", "x": 0, "y": 22, "width": 12, "height": 6,
     "properties": {
      "title": "Bedrock Latency by Model (from timings.bedrock)",
      "query": f"SOURCE '{LOG_GROUP}' | fields @timestamp, @message\n"
               "| parse @message '\"path\": \"*\"' as path\n"
               "| parse @message '\"event\": \"*\"' as event\n"
               "| parse @message '\"timings\": {*}' as timings_raw\n"
               "| filter event = 'request_end' and path like '/model/' and timings_raw like '\"bedrock\"'\n"
               "| parse timings_raw '\"bedrock\": *,' as bedrock_ms\n"
               "| parse timings_raw '\"total\": *}' as total_ms\n"
               "| parse path '/model/*/invoke' as model_path\n"
               "| fields replace(model_path, 'global.anthropic.', '') as model\n"
               "| fields replace(model, 'us.anthropic.', '') as model\n"
               "| fields (total_ms - bedrock_ms) as overhead_ms\n"
               "| stats avg(bedrock_ms) as avg_bedrock, pct(bedrock_ms, 50) as p50_bedrock, pct(bedrock_ms, 90) as p90_bedrock, max(bedrock_ms) as max_bedrock, avg(overhead_ms) as avg_overhead, count() as reqs by model\n"
               "| sort avg_bedrock desc",
      "region": REGION, "stacked": False, "view": "table"}},

    # Slow request buckets
    {"type": "log", "x": 12, "y": 22, "width": 12, "height": 6,
     "properties": {
      "title": "🐌 Slow Bedrock Requests (>10s / >30s / >60s / >120s)",
      "query": f"SOURCE '{LOG_GROUP}' | fields @timestamp, @message\n"
               "| parse @message '\"event\": \"*\"' as event\n"
               "| parse @message '\"path\": \"*\"' as path\n"
               "| parse @message '\"timings\": {*}' as timings_raw\n"
               "| filter event = 'request_end' and path like '/model/' and timings_raw like '\"bedrock\"'\n"
               "| parse timings_raw '\"bedrock\": *,' as bedrock_ms\n"
               "| stats count() as total, sum(case(bedrock_ms > 10000, 1, 0)) as `>10s`, sum(case(bedrock_ms > 30000, 1, 0)) as `>30s`, sum(case(bedrock_ms > 60000, 1, 0)) as `>60s`, sum(case(bedrock_ms > 120000, 1, 0)) as `>120s` by bin(5m)",
      "region": REGION, "stacked": False, "view": "timeSeries"}},

    # Component breakdown
    {"type": "log", "x": 0, "y": 28, "width": 12, "height": 6,
     "properties": {
      "title": "Gateway Component Breakdown (from timings)",
      "query": f"SOURCE '{LOG_GROUP}' | fields @timestamp, @message\n"
               "| parse @message '\"event\": \"*\"' as event\n"
               "| parse @message '\"path\": \"*\"' as path\n"
               "| parse @message '\"timings\": {*}' as timings_raw\n"
               "| filter event = 'request_end' and path like '/model/'\n"
               "| parse timings_raw '\"auth\": *,' as auth_ms\n"
               "| parse timings_raw '\"budget_check\": *,' as budget_ms\n"
               "| parse timings_raw '\"ratelimit_check\": *,' as ratelimit_ms\n"
               "| parse timings_raw '\"bedrock\": *,' as bedrock_ms\n"
               "| parse timings_raw '\"total\": *}' as total_ms\n"
               "| stats avg(auth_ms) as avg_auth, avg(budget_ms) as avg_budget, avg(ratelimit_ms) as avg_ratelimit, avg(bedrock_ms) as avg_bedrock, avg(total_ms) as avg_total, count() as requests by bin(5m)",
      "region": REGION, "stacked": False, "view": "timeSeries"}},

    # Streaming vs non-streaming
    {"type": "log", "x": 12, "y": 28, "width": 12, "height": 6,
     "properties": {
      "title": "Streaming vs Non-Streaming Latency",
      "query": f"SOURCE '{LOG_GROUP}' | fields @timestamp, @message\n"
               "| parse @message '\"latency_ms\": *,' as latency_ms\n"
               "| parse @message '\"event\": \"*\"' as event\n"
               "| parse @message '\"path\": \"*\"' as path\n"
               "| filter event = 'request_end' and path like '/model/'\n"
               "| fields case(path like 'invoke-with-response-stream', 'streaming', 1, 'non-streaming') as req_type\n"
               "| stats avg(latency_ms) as avg_ms, pct(latency_ms, 50) as p50_ms, pct(latency_ms, 95) as p95_ms, count() as requests by req_type, bin(5m)",
      "region": REGION, "stacked": False, "view": "timeSeries"}},

    # X-Ray
    {"type": "text", "x": 0, "y": 34, "width": 24, "height": 1,
     "properties": {"markdown": "## 📊 X-Ray Traces"}},
    {"type": "metric", "x": 0, "y": 35, "width": 8, "height": 6,
     "properties": {"title": "X-Ray Trace Count",
      "metrics": [["AWS/X-Ray", "ApproximateTraceCount", "GroupName", "Default", {"stat": "Sum", "label": "Traces"}]],
      "view": "timeSeries", "stacked": False, "region": REGION, "period": 60}},
    {"type": "log", "x": 8, "y": 35, "width": 16, "height": 6,
     "properties": {
      "title": "Slowest Requests (last 1h) — with Bedrock vs Overhead",
      "query": f"SOURCE '{LOG_GROUP}' | fields @timestamp, @message\n"
               "| parse @message '\"latency_ms\": *,' as latency_ms\n"
               "| parse @message '\"event\": \"*\"' as event\n"
               "| parse @message '\"path\": \"*\"' as path\n"
               "| parse @message '\"status_code\": *,' as status\n"
               "| parse @message '\"timings\": {*}' as timings_raw\n"
               "| filter event = 'request_end' and path like '/model/'\n"
               "| parse timings_raw '\"bedrock\": *,' as bedrock_ms\n"
               "| fields (latency_ms - bedrock_ms) as overhead_ms\n"
               "| sort latency_ms desc\n"
               "| limit 10\n"
               "| fields @timestamp, latency_ms, bedrock_ms, overhead_ms, status, path",
      "region": REGION, "stacked": False, "view": "table"}},

    # Pod Health
    {"type": "text", "x": 0, "y": 41, "width": 24, "height": 1,
     "properties": {"markdown": "## 🖥️ Pod Health (Container Insights)"}},
    {"type": "metric", "x": 0, "y": 42, "width": 8, "height": 6,
     "properties": {"title": "Pod CPU Utilization",
      "metrics": [["ContainerInsights", "pod_cpu_utilization", "PodName", "bedrockgateway", "ClusterName", "bedrockgw-dev-eks-cluster", "Namespace", "bedrockgw", {"stat": "Average", "label": "CPU %"}]],
      "view": "timeSeries", "stacked": False, "region": REGION, "period": 60, "yAxis": {"left": {"label": "%", "showUnits": False}}}},
    {"type": "metric", "x": 8, "y": 42, "width": 8, "height": 6,
     "properties": {"title": "Pod Memory Utilization",
      "metrics": [["ContainerInsights", "pod_memory_utilization", "PodName", "bedrockgateway", "ClusterName", "bedrockgw-dev-eks-cluster", "Namespace", "bedrockgw", {"stat": "Average", "label": "Memory %"}]],
      "view": "timeSeries", "stacked": False, "region": REGION, "period": 60, "yAxis": {"left": {"label": "%", "showUnits": False}}}},
    {"type": "metric", "x": 16, "y": 42, "width": 8, "height": 6,
     "properties": {"title": "Pod Network (Rx/Tx bytes)",
      "metrics": [
        ["ContainerInsights", "pod_network_rx_bytes", "PodName", "bedrockgateway", "ClusterName", "bedrockgw-dev-eks-cluster", "Namespace", "bedrockgw", {"stat": "Average", "label": "Rx bytes/s"}],
        ["ContainerInsights", "pod_network_tx_bytes", "PodName", "bedrockgateway", "ClusterName", "bedrockgw-dev-eks-cluster", "Namespace", "bedrockgw", {"stat": "Average", "label": "Tx bytes/s"}]],
      "view": "timeSeries", "stacked": False, "region": REGION, "period": 60}},
]

dashboard_body = json.dumps({"widgets": widgets})

result = subprocess.run(
    ["aws", "cloudwatch", "put-dashboard",
     "--dashboard-name", DASH_NAME,
     "--dashboard-body", dashboard_body,
     "--region", REGION],
    capture_output=True, text=True
)
print(result.stdout)
if result.returncode != 0:
    print(f"ERROR: {result.stderr}", file=sys.stderr)
    sys.exit(1)
print(f"Dashboard '{DASH_NAME}' deployed successfully!")
print(f"View at: https://{REGION}.console.aws.amazon.com/cloudwatch/home?region={REGION}#dashboards/dashboard/{DASH_NAME}")
