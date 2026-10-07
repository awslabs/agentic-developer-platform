# App-owned evidence storage, composed with the existing deployment identities.
module "gateway_deployment_evidence" {
  source      = "../../modules/gateway/infra/deployment-evidence"
  account_id  = data.aws_caller_identity.current.account_id
  environment = var.environment
  reader_role = "adp-${var.environment}-role-gateway-service"
  # D2/D3 reconciliation runs in the tick Lambda as well as the Gateway API.
  additional_reader_roles = var.enable_gateway_backend_deployment ? ["adp-${var.environment}-orchestration-tick-role"] : []
  writer_roles = toset(concat(
    [aws_iam_role.frontend_deployment.name],
    [for role in aws_iam_role.gateway_backend : role.name],
  ))
}
output "deployment_evidence_bucket" { value = module.gateway_deployment_evidence.bucket_name }
