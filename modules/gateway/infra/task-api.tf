# Additive Task API prerequisites; preparation does not enable admission/workers.
variable "task_api_prerequisites_enabled" {
  type        = bool
  default     = false
  description = "Prepare the gateway-only Task API policy after reviewing exact shared table/bucket bindings."
}

variable "task_api_artifact_bucket_name" {
  type        = string
  default     = ""
  description = "Existing encrypted private artifact bucket; Task API uses only its tasks/ prefix."
}

locals {
  task_api_policy = templatefile("${path.module}/policies/task-api.json.tftpl", {
    request_table_arn   = "arn:${data.aws_partition.current.partition}:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/${local.worker_events_table}"
    authority_table_arn = "arn:${data.aws_partition.current.partition}:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/adp-${var.environment}-agent-authority"
    artifact_bucket_arn = "arn:${data.aws_partition.current.partition}:s3:::${var.task_api_artifact_bucket_name}"
    dynamodb_key_arn    = local.worker_events_key
    region              = var.aws_region
    account_id          = data.aws_caller_identity.current.account_id
  })
}

resource "aws_iam_policy" "gateway_task_api" {
  count       = var.task_api_prerequisites_enabled ? 1 : 0
  name        = "adp-${var.environment}-policy-gateway-task-api"
  description = "Task records, exact sparse work index, protected grants and tasks/ artifact objects"
  policy      = local.task_api_policy

  lifecycle {
    precondition {
      condition     = var.task_api_artifact_bucket_name != "" && local.worker_events_table != "" && local.worker_events_key != ""
      error_message = "Task API preparation requires real artifact bucket, request-table and DynamoDB KMS bindings."
    }
  }
}

resource "aws_iam_role_policy_attachment" "gateway_task_api" {
  count      = var.task_api_prerequisites_enabled ? 1 : 0
  role       = local.gateway_service_irsa_role_name
  policy_arn = aws_iam_policy.gateway_task_api[0].arn
}

variable "task_api_flags" {
  type = object({
    admission = optional(bool, false)
    read      = optional(bool, false)
    worker    = optional(bool, false)
    recovery  = optional(bool, false)
  })
  default     = {}
  description = "Explicit operator rollout gates; all remain off during prerequisite preparation."
}

locals {
  task_api_config = merge(
    { for flag, enabled in var.task_api_flags : "task-api-${flag}-enabled" => tostring(enabled) },
    { "task-artifact-bucket-name" = var.task_api_artifact_bucket_name }
  )
}

resource "aws_ssm_parameter" "task_api_config" {
  for_each = var.task_api_prerequisites_enabled ? local.task_api_config : {}
  name     = "/adp/${var.environment}/gateway/${each.key}"
  type     = "String"
  value    = each.value
}
