# Runner authentication values are written by the authorized operator, never Terraform.
locals {
  gitlab_runner_groups = { for group_id in var.runner_group_ids : tostring(group_id) => group_id }
}

resource "aws_secretsmanager_secret" "runner_group" {
  for_each                = local.gitlab_runner_groups
  name                    = "adp/${var.environment}/gitlab-runner/${each.key}"
  description             = "Group-scoped GitLab Runner authentication token (operator populated)"
  recovery_window_in_days = 30
  tags                    = merge(local.common_tags, { Component = "gitlab-runner", GroupId = each.key })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_ssm_parameter" "runner_operation" {
  for_each    = local.gitlab_runner_groups
  name        = "/adp/${var.environment}/gitlab-runner-operations/${each.key}"
  description = "Non-secret runner registration phase (operator maintained)"
  type        = "String"
  value       = "{}"
  tags        = local.common_tags

  lifecycle {
    ignore_changes = [value]
  }
}

data "aws_iam_role" "runner_registrar" {
  count = var.runner_registrar_role_name != "" ? 1 : 0
  name  = var.runner_registrar_role_name
}

data "aws_iam_role" "runner_ca_exporter" {
  count = var.runner_ca_exporter_role_name != "" ? 1 : 0
  name  = var.runner_ca_exporter_role_name
}

resource "aws_secretsmanager_secret" "runner_ca" {
  name                    = "adp/${var.environment}/gitlab/ca-chain"
  description             = "Operator-validated CA chain for private runner TLS"
  recovery_window_in_days = 30
  tags                    = local.common_tags
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_secretsmanager_secret_policy" "runner_group" {
  for_each   = local.gitlab_runner_groups
  secret_arn = aws_secretsmanager_secret.runner_group[each.key].arn
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Sid       = "DenyOtherGroupReads", Effect = "Deny", Principal = "*",
      Action    = ["secretsmanager:GetSecretValue"], Resource = "*",
      Condition = { ArnNotEquals = { "aws:PrincipalArn" = compact([try(data.aws_iam_role.runner_registrar[0].arn, "arn:aws:iam::000000000000:role/adp-unconfigured"), lookup(var.runner_manager_role_arns, each.key, "")]) } }
    },
    {
      Sid       = "DenyUntrustedWrites", Effect = "Deny", Principal = "*",
      Action    = ["secretsmanager:PutSecretValue", "secretsmanager:UpdateSecretVersionStage", "secretsmanager:DeleteSecret", "secretsmanager:RestoreSecret", "secretsmanager:UpdateSecret", "secretsmanager:RotateSecret"], Resource = "*",
      Condition = { ArnNotEquals = { "aws:PrincipalArn" = [try(data.aws_iam_role.runner_registrar[0].arn, "arn:aws:iam::000000000000:role/adp-unconfigured")] } }
    },
    {
      Sid       = "AllowGroupManagerRead", Effect = "Allow", Principal = "*",
      Action    = ["secretsmanager:GetSecretValue"], Resource = "*",
      Condition = { ArnEquals = { "aws:PrincipalArn" = [lookup(var.runner_manager_role_arns, each.key, "arn:aws:iam::000000000000:role/adp-unconfigured")] } }
    }
  ] })
  lifecycle {
    precondition {
      condition     = var.runner_registrar_role_name != "" && length(setsubtract(toset(keys(var.runner_manager_role_arns)), toset(keys(local.gitlab_runner_groups)))) == 0
      error_message = "Runner groups require an isolated registrar and manager roles must map to configured groups."
    }
    precondition {
      condition     = alltrue([for arn in values(var.runner_manager_role_arns) : arn != try(data.aws_iam_role.runner_registrar[0].arn, "") && arn != try(data.aws_iam_role.runner_ca_exporter[0].arn, "") && arn != aws_iam_role.gitlab.arn && split(":", arn)[4] == data.aws_caller_identity.current.account_id && !contains(var.portal_operator_role_arns, arn)])
      error_message = "Managers cannot use operator, exporter or instance roles."
    }
  }
}

resource "aws_secretsmanager_secret_policy" "runner_ca" {
  secret_arn = aws_secretsmanager_secret.runner_ca.arn
  lifecycle {
    precondition {
      condition     = length(keys(local.gitlab_runner_groups)) > 0 || length(var.runner_manager_role_arns) == 0
      error_message = "CA access cannot be granted to managers without configured runner groups."
    }
  }
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    {
      Sid       = "DenyUntrustedCaReads", Effect = "Deny", Principal = "*",
      Action    = ["secretsmanager:GetSecretValue"], Resource = "*",
      Condition = { ArnNotEquals = { "aws:PrincipalArn" = concat(["arn:aws:iam::000000000000:role/adp-unconfigured"], values(var.runner_manager_role_arns), [for role in data.aws_iam_role.runner_registrar : role.arn], [for role in data.aws_iam_role.runner_ca_exporter : role.arn]) } }
    },
    {
      Sid       = "DenyUntrustedCaWrites", Effect = "Deny", Principal = "*",
      Action    = ["secretsmanager:PutSecretValue", "secretsmanager:DeleteSecret", "secretsmanager:RestoreSecret", "secretsmanager:UpdateSecret", "secretsmanager:RotateSecret"], Resource = "*",
      Condition = { ArnNotEquals = { "aws:PrincipalArn" = concat(["arn:aws:iam::000000000000:role/adp-unconfigured"], [for role in data.aws_iam_role.runner_ca_exporter : role.arn]) } }
    }
    ], length(var.runner_manager_role_arns) > 0 ? [{
      Sid       = "AllowManagerCaRead", Effect = "Allow", Principal = "*",
      Action    = ["secretsmanager:GetSecretValue"], Resource = "*",
      Condition = { ArnEquals = { "aws:PrincipalArn" = values(var.runner_manager_role_arns) } }
  }] : []) })
}

resource "aws_iam_role_policy" "runner_registrar" {
  count = var.runner_registrar_role_name != "" ? 1 : 0
  name  = "${local.name_prefix}-runner-registrar"
  role  = data.aws_iam_role.runner_registrar[0].name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.reconciler_admin.arn },
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue", "secretsmanager:DescribeSecret", "secretsmanager:UpdateSecretVersionStage", "secretsmanager:DeleteSecret"], Resource = [for secret in aws_secretsmanager_secret.runner_group : secret.arn] },
    { Effect = "Allow", Action = ["ssm:GetParameter", "ssm:PutParameter"], Resource = [for parameter in aws_ssm_parameter.runner_operation : parameter.arn] },
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.runner_ca.arn },
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = [
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/gitlab/url",
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/gitlab/runner-url",
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/gitlab/ca-chain-secret-arn"
    ] }
  ] })
  lifecycle {
    precondition {
      condition     = contains(concat(var.portal_operator_role_arns, var.runner_registrar_admin_role_arns), data.aws_iam_role.runner_registrar[0].arn)
      error_message = "Registrar must be explicitly admitted by a portal or registrar-only admin-secret allowlist."
    }
    precondition {
      condition     = var.runner_registrar_role_name != var.runner_ca_exporter_role_name
      error_message = "Registrar and CA exporter must have separate roles."
    }
  }
}

resource "aws_iam_role_policy" "runner_ca_exporter" {
  count = var.runner_ca_exporter_role_name != "" ? 1 : 0
  name  = "${local.name_prefix}-runner-ca-exporter"
  role  = data.aws_iam_role.runner_ca_exporter[0].name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["acm:GetCertificate"], Resource = var.certificate_arn },
    { Effect = "Allow", Action = ["secretsmanager:PutSecretValue", "secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.runner_ca.arn },
    { Effect = "Deny", Action = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"], Resource = concat([aws_secretsmanager_secret.reconciler_admin.arn], [for secret in aws_secretsmanager_secret.runner_group : secret.arn]) }
  ] })
  lifecycle {
    precondition {
      condition     = var.certificate_arn != "" && !contains(var.portal_operator_role_arns, data.aws_iam_role.runner_ca_exporter[0].arn)
      error_message = "CA exporter requires a certificate and cannot be an admin operator."
    }
  }
}
