# Optional, exact additional provider access for the existing management Gateway.
# The provider registry remains owned by provider-authority in the management region.
variable "gateway_access_enabled" {
  type    = bool
  default = false
}
resource "aws_iam_policy" "gateway_access" {
  count = var.gateway_access_enabled ? 1 : 0
  name  = "${aws_iam_role.provider.name}-gateway-access"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["sts:AssumeRole", "sts:TagSession"], Resource = aws_iam_role.provider.arn },
      { Effect = "Allow", Action = ["iam:GetRole", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:GetRolePolicy"], Resource = aws_iam_role.provider.arn },
      { Effect = "Allow", Action = ["iam:GetPolicy", "iam:GetPolicyVersion"], Resource = concat([aws_iam_policy.child_boundary.arn], [for policy in aws_iam_policy.execution : policy.arn]) },
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"], Resource = aws_secretsmanager_secret.external_id.arn }
    ]
  })
  depends_on = [terraform_data.verified_gateway]
}
resource "aws_iam_role_policy_attachment" "gateway_access" {
  count      = var.gateway_access_enabled ? 1 : 0
  role       = basename(var.gateway_role_arn)
  policy_arn = aws_iam_policy.gateway_access[0].arn
}
