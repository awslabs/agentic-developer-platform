# Scoped, opt-in producer wiring; does not authorize an infrastructure apply.
variable "gitlab_model_policy_enabled" {
  type    = bool
  default = false
}
resource "aws_iam_role_policy" "lambda_gitlab_model_root" {
  count = var.gitlab_model_policy_enabled ? 1 : 0
  name  = "${local.name_prefix}-gitlab-model-root"
  role  = aws_iam_role.lambda_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["execute-api:Invoke"]
      Resource = [replace(local.work_claim_admission_arn, "/work/admit", "/roots/admit")]
    }]
  })
}
