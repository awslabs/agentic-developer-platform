locals {
  legacy_lookup_gateway = try(regex("^https://([a-z0-9]+)\\.execute-api\\.([a-z0-9-]+)\\.amazonaws\\.com/([A-Za-z0-9_-]+)$", var.gateway_api_url), ["", "", ""])
}

resource "aws_iam_role_policy" "lambda_legacy_identity_lookups" {
  count = local.legacy_lookup_gateway[0] != "" ? 1 : 0
  name  = "${local.name_prefix}-legacy-identity-lookups"
  role  = aws_iam_role.lambda_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([{
      Effect   = "Allow"
      Action   = ["execute-api:Invoke"]
      Resource = [for route in ["resolve-installation", "resolve-user"] : "arn:aws:execute-api:${var.aws_region}:${local.account_id}:${local.legacy_lookup_gateway[0]}/${local.legacy_lookup_gateway[2]}/POST/internal/v1/${route}"]
      }], var.internal_api_key_parameter_name != "" ? [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = ["arn:aws:ssm:${var.aws_region}:${local.account_id}:parameter${var.internal_api_key_parameter_name}"]
    }] : [])
  })
  lifecycle {
    precondition {
      condition     = local.legacy_lookup_gateway[1] == var.aws_region
      error_message = "Legacy identity lookups must use this deployment region."
    }
  }
}
