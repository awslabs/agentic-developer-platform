variable "orchestration_agent_authority_prepared" {
  description = "Prepare tick authority IAM independently of enabling protected dispatch. Consumes webhook Terraform's environment-scoped wiring after that module exists."
  type        = bool
  default     = true
}

# Optional discovery avoids a gateway -> webhook -> gateway bootstrap cycle.
# These parameters contain resource identifiers, never credentials.
data "aws_ssm_parameters_by_path" "worker_runtime" {
  path            = "/adp/${var.environment}/webhook-ingress/worker-runtime"
  recursive       = false
  with_decryption = false
}

locals {
  worker_runtime_parameters = zipmap(data.aws_ssm_parameters_by_path.worker_runtime.names, data.aws_ssm_parameters_by_path.worker_runtime.values)
  worker_runtime_wiring = jsondecode(lookup(
    local.worker_runtime_parameters,
    "/adp/${var.environment}/webhook-ingress/worker-runtime/wiring",
    "{}"
  ))
  worker_events_table = var.orchestration_webhook_events_table != "" ? var.orchestration_webhook_events_table : try(local.worker_runtime_wiring.webhook_events_table, "")
  worker_events_key   = var.orchestration_webhook_events_kms_key_arn != "" ? var.orchestration_webhook_events_kms_key_arn : try(local.worker_runtime_wiring.webhook_events_kms_key_arn, "")
  worker_queue_arn    = var.orchestration_dispatch_queue_arn != "" ? var.orchestration_dispatch_queue_arn : try(local.worker_runtime_wiring.dispatch_queue_arn, "")
  worker_queue_url    = var.orchestration_dispatch_queue_url != "" ? var.orchestration_dispatch_queue_url : try(local.worker_runtime_wiring.dispatch_queue_url, "")
}
