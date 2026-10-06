# The app owns this one-object read grant and its attachment, never the shared
# Gateway role. A managed policy avoids the role's fixed inline-policy size cap.
locals {
  gateway_route_policy_name = "adp-${var.environment}-superplane-gateway-route-read"
  gateway_route_policy_arn  = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/${local.gateway_route_policy_name}"
}

resource "aws_iam_policy" "gateway_route_read" {
  name = local.gateway_route_policy_name
  path = "/"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = "arn:aws:s3:::adp-terraform-state-${data.aws_caller_identity.current.account_id}/domain-routes/${var.environment}/superplane/public-route.json"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "gateway_route_read" {
  role       = data.terraform_remote_state.platform.outputs.gateway_service_irsa_role_name
  policy_arn = local.gateway_route_policy_arn
  # Keep the exact ARN known for ownership review while ordering policy creation.
  depends_on = [aws_iam_policy.gateway_route_read]
}
