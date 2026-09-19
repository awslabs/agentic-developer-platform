# Stage 1 producer admission is authenticated using the verified webhook role.
# This grants only the gateway's actual API/stage/method/path, never /agent/* or
# arbitrary internal endpoints. It is provisioned before enabling admission.
locals {
  work_claim_gateway = regex(
    "^https://([a-z0-9]+)\\.execute-api\\.([a-z0-9-]+)\\.amazonaws\\.com/([A-Za-z0-9_-]+)/?$",
    data.aws_ssm_parameter.gateway_apigw_invoke_url.value
  )
  work_claim_admission_arn = "arn:aws:execute-api:${var.aws_region}:${local.account_id}:${local.work_claim_gateway[0]}/${local.work_claim_gateway[2]}/POST/internal/v1/agent/work/admit"
}

resource "aws_iam_role_policy" "lambda_work_claim_admission" {
  count = local.agent_authority_provisioned ? 1 : 0
  name  = "${local.name_prefix}-policy-ingress-work-admission"
  role  = aws_iam_role.lambda_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["execute-api:Invoke"]
      Resource = [local.work_claim_admission_arn]
    }]
  })

  lifecycle {
    precondition {
      condition     = local.work_claim_gateway[1] == var.aws_region
      error_message = "The Stage 1 admission endpoint must be the gateway in this deployment region."
    }
  }
}
