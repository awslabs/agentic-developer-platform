locals {
  runtime      = jsondecode(file("${path.module}/../../releases/browser-runtime.json"))
  broker_image = var.broker_image != "" ? var.broker_image : "${var.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${local.runtime.repository}@${local.runtime.digest}"
}

# App-owned configuration consumed by the generic hosted-worker interfaces.
output "worker_environment" {
  value = merge({
    URL_ANALYSIS_EVIDENCE_BUCKET = "adp-${var.environment}-url-analysis-evidence-v2-${var.account_id}"
    URL_ANALYSIS_BROWSER_MODE    = var.browser_mode
    }, var.browser_mode == "broker" ? {
    URL_ANALYSIS_BROWSER_BROKER = "http://url-analysis-browser-broker.${var.namespace}.svc.cluster.local:8765"
    } : {}, local.cc_enabled ? {
    CYBER_CC_DATABASE  = aws_glue_catalog_database.common_crawl[0].name
    CYBER_CC_TABLE     = aws_glue_catalog_table.common_crawl[0].name
    CYBER_CC_WORKGROUP = aws_athena_workgroup.common_crawl[0].name
    CYBER_CC_CRAWLS    = join(",", var.common_crawl_partitions)
    CYBER_CC_REGION    = var.aws_region
    } : {}, var.tools_endpoint != "" ? {
    ADP_CYBER_TOOLS_ENDPOINT = var.tools_endpoint
    ADP_TASK_TOOL_ROUTES = jsonencode(merge(
      { for op in ["triage", "static", "dynamic", "result", "url_analysis", "enrich"] : "cyber.${op}" => var.tools_endpoint },
      var.task_url_tools_enabled ? merge(
        { for op in ["common_crawl_scan", "common_crawl_result", "common_crawl_read"] : "cyber.${op}" => "${var.tools_endpoint}/common-crawl" },
        { for op in ["browser_start", "browser_step", "browser_close", "browser_inspect", "browser_cleanup"] : "cyber.${op}" => "local:cyber_tools.task_browser.TaskBrowser" }
      ) : {}
    ))
    ADP_TASK_TOOL_CLEANUP = jsonencode(var.task_url_tools_enabled ? ["cyber.browser_cleanup"] : [])
  } : {})
}

output "worker_artifact_resources" {
  value = ["arn:aws:s3:::adp-*-url-analysis-evidence-v2-*/*"]
}

output "worker_egress" {
  value = var.browser_mode == "broker" ? [{
    pod_labels = { "app.kubernetes.io/name" = "url-analysis-browser-broker" }
    port       = 8765
    protocol   = "TCP"
  }] : []
}

# Included in both the protected worker policy and its permissions boundary.
# Legacy administrator/source roles keep their existing direct-browser deny.
output "worker_browser_permissions" {
  value = var.browser_mode == "native" ? [{
    Sid      = "DirectAgentCoreBrowser"
    Effect   = "Allow"
    Action   = local.url_analysis_browser_actions
    Resource = "*"
    Condition = {
      StringEquals = { "aws:RequestedRegion" = var.aws_region }
    }
  }] : []
}
