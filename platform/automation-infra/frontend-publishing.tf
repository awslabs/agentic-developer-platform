# The frontend can be published without granting its runner the infrastructure
# deployment role. This identity owns only the existing SPA bucket and
# CloudFront distribution resolved from the deployed gateway's SSM parameters.
data "aws_ssm_parameter" "frontend_bucket" {
  name = "/adp/${var.environment}/gateway/frontend-bucket"
}

data "aws_ssm_parameter" "frontend_cloudfront_id" {
  name = "/adp/${var.environment}/gateway/cloudfront-id"
}

locals {
  # These are public deployment identifiers, stored as String parameters.
  frontend_bucket        = nonsensitive(data.aws_ssm_parameter.frontend_bucket.value)
  frontend_cloudfront_id = nonsensitive(data.aws_ssm_parameter.frontend_cloudfront_id.value)
}

resource "aws_iam_role" "frontend_deployment" {
  name                 = "${var.name_prefix}-frontend-trusted-deployment"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "repo:${var.repository}:environment:adp-frontend-deploy-${var.environment}"
      } }
    }]
  })
}

resource "aws_iam_role_policy" "frontend_deployment" {
  lifecycle {
    precondition {
      condition = (
        can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", local.frontend_bucket)) &&
        can(regex("^E[A-Z0-9]{8,}$", local.frontend_cloudfront_id))
      )
      error_message = "Frontend bucket or CloudFront distribution SSM value is invalid."
    }
  }
  name = "publish-existing-gateway-frontend"
  role = aws_iam_role.frontend_deployment.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadFrontendBuildConfiguration"
        Effect = "Allow"
        Action = ["ssm:GetParameter"]
        Resource = [for name in [
          "frontend-bucket", "cloudfront-id", "cloudfront-domain",
          "cognito-user-pool-id", "cognito-client-id", "cognito-domain",
          "github-auth-broker-url", "agent-ws-url",
        ] : "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/gateway/${name}"]
      },
      {
        Sid      = "ListFrontendObjects"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = "arn:aws:s3:::${local.frontend_bucket}"
      },
      {
        Sid      = "PublishFrontendObjects"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:DeleteObject"]
        Resource = "arn:aws:s3:::${local.frontend_bucket}/*"
      },
      {
        Sid      = "RefreshFrontendCache"
        Effect   = "Allow"
        Action   = ["cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"]
        Resource = "arn:aws:cloudfront::${data.aws_caller_identity.current.account_id}:distribution/${local.frontend_cloudfront_id}"
      },
    ]
  })
}

output "frontend_deployment_role_arn" {
  value = aws_iam_role.frontend_deployment.arn
}
