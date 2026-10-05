# Task recovery is independent of orchestration/pricing schedules. The rule and
# adapters remain default-off until the storage/IAM/consumer evidence is complete.
resource "aws_lambda_alias" "task_recovery" {
  count            = local.agent_authority_provisioned ? 1 : 0
  name             = "task-recovery"
  description      = "Restricted Task API recovery entry point"
  function_name    = aws_lambda_function.github_webhook.function_name
  function_version = aws_lambda_function.github_webhook.version
}

resource "aws_cloudwatch_event_rule" "task_recovery" {
  count               = local.agent_authority_provisioned ? 1 : 0
  name                = "${local.name_prefix}-task-recovery"
  description         = "Bounded Task API publication and settlement recovery"
  schedule_expression = "rate(1 minute)"
  state               = var.task_api_recovery_enabled ? "ENABLED" : "DISABLED"

  lifecycle {
    precondition {
      condition     = !var.task_api_recovery_enabled || var.task_api_worker_enabled
      error_message = "Task recovery requires the Task API worker runtime to be enabled."
    }
  }
}

resource "aws_cloudwatch_event_target" "task_recovery" {
  count     = local.agent_authority_provisioned ? 1 : 0
  rule      = aws_cloudwatch_event_rule.task_recovery[0].name
  target_id = "task-recovery-alias"
  arn       = aws_lambda_alias.task_recovery[0].arn
}

resource "aws_lambda_permission" "task_recovery" {
  count         = local.agent_authority_provisioned ? 1 : 0
  statement_id  = "AllowTaskRecoveryScheduleInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.github_webhook.function_name
  qualifier     = aws_lambda_alias.task_recovery[0].name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.task_recovery[0].arn
}

locals {
  task_adapter_arns = [
    for path in [
      "dispatch/claim",
      "dispatch/settle",
      "recovery/claim",
      "recovery/settle",
    ] : "arn:aws:execute-api:${var.aws_region}:${local.account_id}:${local.work_claim_gateway[0]}/${local.work_claim_gateway[2]}/POST/internal/v1/tasks/${path}"
  ]
}

resource "aws_iam_role_policy" "lambda_task_adapters" {
  count = local.agent_authority_provisioned ? 1 : 0
  name  = "${local.name_prefix}-policy-task-adapters"
  role  = aws_iam_role.lambda_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["execute-api:Invoke"]
      Resource = local.task_adapter_arns
    }]
  })
}

# Gateway Task storage permissions are owned by gateway_task_storage in iam.tf.
# Keep a single policy owner so this path cannot reintroduce table-wide access.

variable "task_api_worker_enabled" {
  type        = bool
  default     = false
  description = "Task API worker runtime readiness; independent of legacy agent authority rollout."
}
