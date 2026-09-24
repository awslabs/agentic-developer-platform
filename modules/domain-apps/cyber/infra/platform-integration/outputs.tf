locals {
  runtime      = jsondecode(file("${path.module}/../../releases/browser-runtime.json"))
  broker_image = var.broker_image != "" ? var.broker_image : "${var.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${local.runtime.repository}@${local.runtime.digest}"
}

# App-owned configuration consumed by the generic hosted-worker interfaces.
output "worker_environment" {
  value = merge({
    URL_ANALYSIS_EVIDENCE_BUCKET = "adp-${var.environment}-url-analysis-evidence-v2-${var.account_id}"
    URL_ANALYSIS_BROWSER_BROKER  = "http://url-analysis-browser-broker.${var.namespace}.svc.cluster.local:8765"
    }, local.cc_enabled ? {
    CYBER_CC_DATABASE  = aws_glue_catalog_database.common_crawl[0].name
    CYBER_CC_TABLE     = aws_glue_catalog_table.common_crawl[0].name
    CYBER_CC_WORKGROUP = aws_athena_workgroup.common_crawl[0].name
    CYBER_CC_CRAWLS    = join(",", var.common_crawl_partitions)
    CYBER_CC_REGION    = var.aws_region
  } : {})
}

output "worker_artifact_resources" {
  value = ["arn:aws:s3:::adp-*-url-analysis-evidence-v2-*/*"]
}

output "worker_egress" {
  value = [{
    pod_labels = { "app.kubernetes.io/name" = "url-analysis-browser-broker" }
    port       = 8765
    protocol   = "TCP"
  }]
}
