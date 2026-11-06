# CloudFront and API Gateway share origin provenance without introducing a
# CloudFront -> API Gateway -> Cognito -> CloudFront dependency cycle.
# Preserve the existing proof: this source move must not rotate a deployed key.
moved {
  from = module.api_gateway[0].random_password.edge_provenance
  to   = random_password.edge_provenance[0]
}

resource "random_password" "edge_provenance" {
  count   = var.enable_api_gateway ? 1 : 0
  length  = 64
  special = false
}
