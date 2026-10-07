# Composition only. Shared state and protected registration ownership are retained.
variable "domain_operation_bindings" {
  description = "Reviewed native Superplane runtime binding; empty by default. IDs/role IDs and secret ARNs come from verified owner preparation, never request bodies."
  type = map(object({
    operator_role_arn   = string
    producer_role_arn   = string
    producer_role_id    = string
    worker_role_arn     = string
    worker_role_id      = string
    secret_kms_key_arns = optional(set(string), [])
    binding = object({
      domain                           = string
      org_id                           = string
      adp_org_id                       = string
      producer_registry_id             = string
      worker_registry_id               = string
      database_secret_id               = string
      database_schema                  = string
      domain_database_secret_id        = string
      domain_database_schema           = string
      queue_url                        = string
      worker_namespace                 = string
      worker_service_account           = string
      worker_container                 = string
      worker_scaled_job                = string
      worker_image_digests             = set(string)
      repo                             = string
      observation_url                  = string
      observation_credential_secret_id = string
    })
  }))
  default = {}
}

module "superplane_operation_authority" {
  source                             = "../../../domain-apps/superplane/infra/shared-operation-authority"
  domain_operation_bindings          = var.domain_operation_bindings
  account_id                         = local.account_id
  caller_arn                         = data.aws_caller_identity.current.arn
  environment                        = var.environment
  aws_region                         = var.aws_region
  gateway_namespace                  = var.gateway_namespace
  agent_authority_provisioned        = local.agent_authority_provisioned
  gateway_authority_managed_policies = var.gateway_authority_managed_policies
}
locals {
  domain_operation_enabled          = length(var.domain_operation_bindings) > 0
  domain_operation_gateway_bindings = module.superplane_operation_authority.gateway_bindings
}
output "domain_operation_binding_revisions" {
  description = "Canonical configured binding revision; independent live proof is still required. No readiness is asserted."
  value       = module.superplane_operation_authority.domain_operation_binding_revisions
}

moved {
  from = terraform_data.domain_operation_registration
  to   = module.superplane_operation_authority.terraform_data.domain_operation_registration
}

moved {
  from = aws_iam_role_policy.gateway_domain_operations
  to   = module.superplane_operation_authority.aws_iam_role_policy.gateway_domain_operations
}

moved {
  from = aws_iam_policy.gateway_domain_operations
  to   = module.superplane_operation_authority.aws_iam_policy.gateway_domain_operations
}

moved {
  from = aws_iam_role_policy_attachment.gateway_domain_operations
  to   = module.superplane_operation_authority.aws_iam_role_policy_attachment.gateway_domain_operations
}

moved {
  from = kubernetes_role.gateway_domain_operation_read
  to   = module.superplane_operation_authority.kubernetes_role.gateway_domain_operation_read
}

moved {
  from = kubernetes_role_binding.gateway_domain_operation_read
  to   = module.superplane_operation_authority.kubernetes_role_binding.gateway_domain_operation_read
}
