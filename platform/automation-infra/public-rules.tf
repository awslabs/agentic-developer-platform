# Upstream rules are untrusted parser input. This lane stays off deployment
# nodes and can publish only public YARA rules; it cannot obtain a build or
# deployment identity. Its existing cadence does not dispatch security scans.
resource "aws_iam_role" "rules" {
  name = "${var.name_prefix}-trusted-rules"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:${var.repository}:environment:adp-rules-${var.environment}"
    } }
  }] })
}
resource "aws_iam_role_policy" "rules" {
  name = "public-yara-rules-only"
  role = aws_iam_role.rules.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Effect = "Allow", Action = ["s3:GetObject"],
      Resource = [
        "arn:aws:s3:::adp-${var.environment}-cape-assets/yara-rules/public/florian-roth/*",
        "arn:aws:s3:::adp-${var.environment}-cape-assets/yara-rules/canary-benign/*",
      ]
    },
    {
      Effect   = "Allow", Action = ["s3:PutObject"],
      Resource = "arn:aws:s3:::adp-${var.environment}-cape-assets/yara-rules/public/florian-roth/*"
    },
    {
      Effect    = "Allow", Action = ["s3:ListBucket"],
      Resource  = "arn:aws:s3:::adp-${var.environment}-cape-assets",
      Condition = { StringLike = { "s3:prefix" = ["yara-rules/public/florian-roth/*", "yara-rules/canary-benign/*"] } }
    },
    {
      Effect = "Deny", NotAction = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "sts:GetCallerIdentity"], Resource = "*"
    },
    {
      Effect = "Deny", Action = ["s3:GetObject"],
      NotResource = [
        "arn:aws:s3:::adp-${var.environment}-cape-assets/yara-rules/public/florian-roth/*",
        "arn:aws:s3:::adp-${var.environment}-cape-assets/yara-rules/canary-benign/*",
      ]
    },
    {
      Effect      = "Deny", Action = ["s3:PutObject"],
      NotResource = "arn:aws:s3:::adp-${var.environment}-cape-assets/yara-rules/public/florian-roth/*"
    },
    {
      Effect      = "Deny", Action = ["s3:ListBucket"],
      NotResource = "arn:aws:s3:::adp-${var.environment}-cape-assets"
    },
    {
      Effect    = "Deny", Action = ["s3:ListBucket"], Resource = "*",
      Condition = { StringNotLike = { "s3:prefix" = ["yara-rules/public/florian-roth/*", "yara-rules/canary-benign/*"] } }
    },
  ] })
}
output "rules_role_arn" { value = aws_iam_role.rules.arn }
