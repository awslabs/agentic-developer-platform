locals {
  group_bindings = {
    for group_id, group in var.groups : group_id => {
      manager_namespace = "gitlab-runner-${group_id}"
      tenant_id         = group.tenant_id
      job_namespace     = "gitlab-jobs-${group_id}"
      token_secret_arn  = group.token_secret_arn
      tags              = group.tags
      access_level      = group.access_level
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "terraform_remote_state" "platform" {
  backend = "s3"
  config = {
    bucket = var.platform_state.bucket
    key    = var.platform_state.key
    region = var.platform_state.region
  }
}

data "aws_ssm_parameter" "runner_url" {
  name            = "/adp/${var.environment}/gitlab/runner-url"
  with_decryption = false
}

data "aws_ssm_parameter" "ca_secret_arn" {
  name            = "/adp/${var.environment}/gitlab/ca-chain-secret-arn"
  with_decryption = false
}

locals {
  private_gitlab_hostname = try(regex("^https://([^/:?#]+)(?:/gitlab)?$", data.aws_ssm_parameter.runner_url.value)[0], "")
}

resource "terraform_data" "discovery" {
  input = {
    cluster_name  = data.terraform_remote_state.platform.outputs.eks_cluster_name
    runner_url    = data.aws_ssm_parameter.runner_url.value
    ca_secret_arn = data.aws_ssm_parameter.ca_secret_arn.value
  }

  lifecycle {
    precondition {
      condition = (
        local.private_gitlab_hostname != "" &&
        can(regex("^([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+[a-z0-9]([a-z0-9-]*[a-z0-9])?$", local.private_gitlab_hostname)) &&
        endswith(local.private_gitlab_hostname, ".${var.gitlab_private_zone}") &&
        can(regex("^arn:aws(-us-gov|-cn)?:secretsmanager:${var.aws_region}:[0-9]{12}:secret:adp/${var.environment}/gitlab/ca-chain-[A-Za-z0-9]{6}$", data.aws_ssm_parameter.ca_secret_arn.value))
      )
      error_message = "Private GitLab HTTPS discovery and exact CA secret reference are required."
    }
  }
}

locals {
  active_group_bindings = var.manager_activation_enabled ? local.group_bindings : {}
}
