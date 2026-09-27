# Browser acceptance needs test-user authentication and fixture diagnostics.
# Keep this separate from the lightweight gateway smoke checks identity.
variable "enable_browser_checks" {
  type    = bool
  default = false
}

data "aws_secretsmanager_secret" "browser_credentials" {
  count = var.enable_browser_checks ? 1 : 0
  name  = "adp/${var.environment}/gateway/test-admin-credentials"
}
data "aws_ssm_parameter" "browser_user_pool" {
  count = var.enable_browser_checks ? 1 : 0
  name  = "/adp/${var.environment}/gateway/cognito-user-pool-id"
}
resource "aws_iam_role" "browser_checks" {
  count = var.enable_browser_checks ? 1 : 0
  name  = "${var.name_prefix}-browser-trusted-checks"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:${var.repository}:environment:adp-browser-checks-${var.environment}"
    } }
  }] })
}
resource "aws_iam_role_policy" "browser_checks" {
  count = var.enable_browser_checks ? 1 : 0
  name  = "browser-acceptance-fixtures"
  role  = aws_iam_role.browser_checks[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = [for name in [
      "frontend-url", "cloudfront-domain", "feature-new-ui", "cognito-user-pool-id", "cognito-client-id"
    ] : "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/gateway/${name}"] },
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = data.aws_secretsmanager_secret.browser_credentials[0].arn },
    { Effect = "Allow", Action = ["cognito-idp:AdminInitiateAuth"], Resource = "arn:aws:cognito-idp:${var.aws_region}:${data.aws_caller_identity.current.account_id}:userpool/${nonsensitive(data.aws_ssm_parameter.browser_user_pool[0].value)}" },
    { Effect = "Allow", Action = ["logs:FilterLogEvents"], Resource = [for group in [
      "/aws/lambda/adp-${var.environment}-agent-gateway-ingest", "/aws/eks/adp-${var.environment}-eks/chat-agent"
    ] : "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:${group}:*"] }
  ] })
}
