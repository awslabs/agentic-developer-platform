# Composition only. The app owns all new Superplane resource definitions and
# enrollment logic. Empty by default; this does not enroll or activate authority.
variable "domain_provider_authority" {
  type = object({
    operator_role_arn   = string
    operator_role_id    = string
    provider_role_arn   = string
    provider_role_id    = string
    secret_arn          = string
    child_boundary_arn  = string
    managed_policy_arns = set(string)
    secret_kms_key_arns = optional(set(string), [])
  })
  default = null
  validation {
    condition     = var.domain_provider_authority == null || contains(keys(var.domain_operation_bindings), "superplane")
    error_message = "Governed provider storage requires the protected Superplane operation binding."
  }
}

module "superplane_provider_authority" {
  count               = var.domain_provider_authority == null ? 0 : 1
  source              = "../../../domain-apps/superplane/infra/provider-authority"
  environment         = var.environment
  region              = var.aws_region
  account_id          = local.account_id
  operator_role_arn   = var.domain_provider_authority.operator_role_arn
  operator_role_id    = var.domain_provider_authority.operator_role_id
  provider_role_arn   = var.domain_provider_authority.provider_role_arn
  provider_role_id    = var.domain_provider_authority.provider_role_id
  secret_arn          = var.domain_provider_authority.secret_arn
  child_boundary_arn  = var.domain_provider_authority.child_boundary_arn
  managed_policy_arns = var.domain_provider_authority.managed_policy_arns
  secret_kms_key_arns = var.domain_provider_authority.secret_kms_key_arns
}
