variable "persona_model_mapping_enabled" {
  description = "Resolve saved persona models before dispatch, independently of worker authority."
  type        = bool
  default     = false
}

variable "persona_model_additional_producer_roles" {
  description = "Other trusted ingress roles allowed to resolve a tenant human's saved model."
  type        = list(string)
  default     = []
}

resource "aws_iam_role_policy" "lambda_persona_model_selection" {
  count = var.persona_model_mapping_enabled ? 1 : 0
  name  = "${local.name_prefix}-persona-model-selection"
  role  = aws_iam_role.lambda_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["execute-api:Invoke"]
      Resource = [replace(local.work_claim_admission_arn, "/work/admit", "/persona-model/resolve")]
    }]
  })
}
