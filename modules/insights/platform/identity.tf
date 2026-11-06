resource "aws_iam_role" "service" {
  for_each             = toset(["emitter", "distiller"])
  name                 = "${local.prefix}-${each.key}"
  permissions_boundary = lookup(var.deployment_role_boundaries, "arn:aws:iam::${var.aws_account_id}:role/${local.prefix}-${each.key}", null)
  lifecycle {
    precondition {
      condition = toset(keys(var.deployment_role_boundaries)) == toset([
        for service in ["emitter", "distiller"] : "arn:aws:iam::${var.aws_account_id}:role/${local.prefix}-${service}"
        ]) && can(regex("^arn:aws:iam::${var.aws_account_id}:policy/", lookup(
          var.deployment_role_boundaries, "arn:aws:iam::${var.aws_account_id}:role/${local.prefix}-${each.key}", ""
      )))
      error_message = "Admit both exact Insights roles with same-account boundaries through platform/automation-infra before deployment."
    }
  }
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = local.oidc_arn }
      Condition = { StringEquals = {
        "${local.oidc_issuer}:sub" = "system:serviceaccount:adp-insights:insights-${each.key}"
        "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
      } }
    }]
  })
}

resource "aws_iam_role_policy" "service" {
  for_each = aws_iam_role.service
  name     = "evidence-access"
  role     = each.value.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = aws_s3_bucket.evidence.arn
        Condition = { StringLike = { "s3:prefix" = each.key == "emitter" ? ["assurance/v1/*/insights/*"] : ["assurance/v1/*"] } }
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.evidence.arn}/assurance/v1/*/insights/*"
      },
      {
        Effect    = "Allow"
        Action    = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource  = aws_kms_key.evidence.arn
        Condition = { StringEquals = { "kms:ViaService" = "s3.${var.aws_region}.amazonaws.com" } }
      }
      ], each.key == "distiller" ? [{
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.evidence.arn}/assurance/v1/*"
        }] : [{
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.evidence.arn}/assurance/v1/l5/insights/contributions/*"
    }])
  })
}
