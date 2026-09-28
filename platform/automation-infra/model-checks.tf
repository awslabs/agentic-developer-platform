# Runtime publication verifies account readiness without accepting agreements or
# changing account registration. Preparation belongs to platform deployment.
variable "runtime_check_model_ids" {
  type    = set(string)
  default = []
  validation {
    condition     = alltrue([for model in var.runtime_check_model_ids : can(regex("^global\\.anthropic\\.[a-z0-9:.-]+$", model))])
    error_message = "Supply exact global Anthropic inference-profile IDs without wildcards."
  }
}
resource "aws_iam_role" "model_checks" {
  count = length(var.runtime_check_model_ids) > 0 ? 1 : 0
  name  = "${var.name_prefix}-model-trusted-checks"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:${var.repository}:environment:adp-model-checks-${var.environment}"
    } }
  }] })
}
resource "aws_iam_role_policy" "model_checks" {
  count = length(var.runtime_check_model_ids) > 0 ? 1 : 0
  name  = "runtime-default-model-readiness"
  role  = aws_iam_role.model_checks[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["bedrock:GetFoundationModelAvailability"], Resource = "*" },
    { Effect = "Allow", Action = ["bedrock:GetInferenceProfile"], Resource = [for model in var.runtime_check_model_ids : "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${model}"] },
    { Effect = "Allow", Action = ["bedrock:InvokeModel"], Resource = flatten([for model in var.runtime_check_model_ids : [
      "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${model}",
      "arn:aws:bedrock:*::foundation-model/${trimprefix(model, "global.")}",
    ]]) }
  ] })
}
