# The domain app publishes a public route object in the shared state bucket.
# Its read grant is installed and removed with Superplane, not with the base EKS
# cluster. The target role is a read-only platform output; this state owns only
# the additional, narrowly scoped inline policy.
resource "aws_iam_role_policy" "gateway_route_read" {
  name = "adp-${var.environment}-superplane-gateway-route-read"
  role = data.terraform_remote_state.platform.outputs.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = "arn:aws:s3:::adp-terraform-state-${data.aws_caller_identity.current.account_id}/domain-routes/${var.environment}/superplane/public-route.json"
    }]
  })
}
