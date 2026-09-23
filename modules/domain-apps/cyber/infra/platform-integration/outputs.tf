locals {
  runtime      = jsondecode(file("${path.module}/../../releases/browser-runtime.json"))
  broker_image = var.broker_image != "" ? var.broker_image : "${var.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${local.runtime.repository}@${local.runtime.digest}"
}

# App-owned configuration consumed by the generic hosted-worker interfaces.
output "worker_environment" {
  value = {
    URL_ANALYSIS_EVIDENCE_BUCKET = "adp-${var.environment}-url-analysis-evidence-v2-${var.account_id}"
  }
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
