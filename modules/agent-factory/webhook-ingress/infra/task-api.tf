locals {
  task_api_admit_arn = "arn:aws:execute-api:${var.aws_region}:${local.account_id}:${local.work_claim_gateway[0]}/${local.work_claim_gateway[2]}/POST/internal/v1/tasks/admit"
}

resource "aws_iam_role_policy" "lambda_task_api_admission" {
  count = var.task_api_admission_enabled ? 1 : 0
  name  = "${local.name_prefix}-policy-task-api-admission"
  role  = aws_iam_role.lambda_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["execute-api:Invoke"]
      Resource = [local.task_api_admit_arn]
    }]
  })

  lifecycle {
    precondition {
      condition     = local.work_claim_gateway[1] == var.aws_region
      error_message = "The Task API admission endpoint must be the gateway in this deployment region."
    }
  }
}
