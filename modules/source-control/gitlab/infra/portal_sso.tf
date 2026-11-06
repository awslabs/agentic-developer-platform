# Optional module owns its identity wiring. No gateway boot-time dependency.
resource "aws_secretsmanager_secret" "gateway_signing_key" {
  name = "adp/${var.environment}/gateway-oidc-signing-key"
  tags = local.common_tags
}

resource "aws_secretsmanager_secret" "reconciler_admin" {
  name = "adp/${var.environment}/gitlab-admin-token"
  tags = local.common_tags
}

data "aws_iam_role" "portal_gateway" {
  count = var.gateway_role_name != "" ? 1 : 0
  name  = var.gateway_role_name
}

locals {
  portal_wiring_enabled = var.gateway_role_name != "" || local.reconciler_enabled
  portal_gateway_arns   = [for role in data.aws_iam_role.portal_gateway : role.arn]
}

# Legacy agent identities may have GetSecretValue on adp/*. Resource policies
# enforce this boundary even where those broad identity policies still exist.
resource "aws_secretsmanager_secret_policy" "gateway_signing_key" {
  count      = local.portal_wiring_enabled ? 1 : 0
  secret_arn = aws_secretsmanager_secret.gateway_signing_key.arn
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Sid       = "DenyUntrustedKeyAccess", Effect = "Deny", Principal = "*",
    Action    = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"], Resource = "*",
    Condition = { ArnNotEquals = { "aws:PrincipalArn" = concat(local.portal_gateway_arns, var.portal_operator_role_arns) } }
  }] })
  lifecycle {
    precondition {
      condition     = length(var.portal_operator_role_arns) > 0
      error_message = "Portal SSO requires explicit operator IAM role ARNs for credential provisioning and recovery."
    }
  }
}

resource "aws_secretsmanager_secret_policy" "reconciler_admin" {
  count      = local.portal_wiring_enabled || var.runner_registrar_role_name != "" ? 1 : 0
  secret_arn = aws_secretsmanager_secret.reconciler_admin.arn
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Sid    = "DenyUntrustedAdminAccess", Effect = "Deny", Principal = "*",
    Action = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"], Resource = "*",
    Condition = { ArnNotEquals = { "aws:PrincipalArn" = concat(
      [aws_iam_role.gitlab.arn], [for role in aws_iam_role.home_reconciler : role.arn], var.portal_operator_role_arns,
      var.runner_registrar_admin_role_arns
    ) } }
  }] })
  lifecycle {
    precondition {
      condition     = alltrue([for arn in var.runner_registrar_admin_role_arns : arn == try(data.aws_iam_role.runner_registrar[0].arn, "")])
      error_message = "Registrar-only admin access must name the configured existing registrar, never another role."
    }
  }
}

resource "aws_iam_role_policy" "gateway_signing_key" {
  count = var.gateway_role_name != "" ? 1 : 0
  name  = "${local.name_prefix}-portal-signing"
  role  = var.gateway_role_name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.gateway_signing_key.arn },
    { Effect = "Deny", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.reconciler_admin.arn }
  ] })
}

resource "aws_iam_role_policy" "instance_portal_sso" {
  name = "${local.name_prefix}-portal-sso"
  role = aws_iam_role.gitlab.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = [
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/gitlab/jwt-public-key",
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/gitlab/url"
    ] },
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"], Resource = aws_secretsmanager_secret.reconciler_admin.arn }
  ] })
}

locals {
  reconciler_enabled   = var.eks_oidc_provider_arn != "" && var.reconciler_database_resource_arn != ""
  reconciler_oidc_host = try(split("oidc-provider/", var.eks_oidc_provider_arn)[1], "")
}

resource "aws_iam_role" "home_reconciler" {
  count = local.reconciler_enabled ? 1 : 0
  name  = "${local.name_prefix}-home-reconciler"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity", Principal = { Federated = var.eks_oidc_provider_arn },
    Condition = { StringEquals = {
      "${local.reconciler_oidc_host}:sub" = "system:serviceaccount:adp-gateway:gitlab-home-reconciler",
      "${local.reconciler_oidc_host}:aud" = "sts.amazonaws.com"
    } }
  }] })
  tags = local.common_tags
}

resource "aws_iam_role_policy" "home_reconciler" {
  count = local.reconciler_enabled ? 1 : 0
  role  = aws_iam_role.home_reconciler[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.reconciler_admin.arn },
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/gitlab/url" },
    { Effect = "Allow", Action = ["rds-db:connect"], Resource = var.reconciler_database_resource_arn }
  ] })
}

output "home_reconciler_role_arn" {
  value = try(aws_iam_role.home_reconciler[0].arn, null)
}
