# Domain apps own their resources and publish only the worker configuration that
# the shared webhook stack needs. A basic platform deploy reads no app state.
data "terraform_remote_state" "cyber" {
  count   = contains(var.enabled_domain_integrations, "cyber") ? 1 : 0
  backend = "s3"
  config = {
    bucket = "adp-terraform-state-${local.account_id}"
    key    = "${var.environment}/modules/cyber-hosted-integration/terraform.tfstate"
    region = var.aws_region
  }
}

locals {
  domain_worker_environment         = try(data.terraform_remote_state.cyber[0].outputs.worker_environment, {})
  domain_worker_artifact_resources  = try(data.terraform_remote_state.cyber[0].outputs.worker_artifact_resources, [])
  domain_worker_egress              = try(data.terraform_remote_state.cyber[0].outputs.worker_egress, [])
  domain_worker_browser_permissions = try(data.terraform_remote_state.cyber[0].outputs.worker_browser_permissions, [])
}
