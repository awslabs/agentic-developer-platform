terraform {
  required_version = ">= 1.9, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "= 6.65.0" }
  }
}

# Full-installation deletion only. The app teardown hook verifies the existing
# owner's state and a delete-only plan before releasing table protection. No
# provisioning data sources are evaluated after provider roles are retired.
variable "environment" { type = string }
variable "region" { type = string }
variable "account_id" { type = string }
variable "operator_role_arn" { type = string }
variable "operator_role_id" { type = string }
variable "provider_role_arn" { type = string }
variable "provider_role_id" { type = string }
variable "secret_arn" { type = string }
variable "child_boundary_arn" { type = string }
variable "managed_policy_arns" { type = set(string) }
variable "secret_kms_key_arns" {
  type    = set(string)
  default = []
}
output "gateway_environment" { value = {} }
