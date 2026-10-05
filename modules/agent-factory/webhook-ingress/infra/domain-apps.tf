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
  domain_worker_environment           = try(data.terraform_remote_state.cyber[0].outputs.worker_environment, {})
  domain_worker_artifact_resources    = try(data.terraform_remote_state.cyber[0].outputs.worker_artifact_resources, [])
  domain_worker_egress                = try(data.terraform_remote_state.cyber[0].outputs.worker_egress, [])
  domain_worker_browser_permissions   = try(data.terraform_remote_state.cyber[0].outputs.worker_browser_permissions, [])
  domain_worker_image                 = try(data.terraform_remote_state.cyber[0].outputs.worker_image, "")
  domain_worker_task_persona_tools    = try(data.terraform_remote_state.cyber[0].outputs.worker_task_persona_tools, {})
  domain_worker_tool_invoke_resources = try(data.terraform_remote_state.cyber[0].outputs.worker_tool_invoke_resources, [])
}

resource "terraform_data" "domain_worker_image_guard" {
  count = length(var.enabled_domain_integrations) > 0 ? 1 : 0
  input = local.domain_worker_image
  lifecycle {
    precondition {
      condition     = length(var.enabled_domain_integrations) == 0 || can(regex("@sha256:[0-9a-f]{64}$", local.domain_worker_image))
      error_message = "Install and publish the Cyber hosted-worker image from its module before enabling the integration."
    }
  }
}
