# Optional Cyber integration with the shared ADP worker. This root owns its
# browser broker, common-crawl resources and IAM policies in Cyber state.
# Platform and webhook state are read-only inputs.
terraform {
  required_version = ">= 1.7"
  backend "s3" {}
  required_providers {
    aws        = { source = "hashicorp/aws", version = ">= 6.42.0, < 7.0.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.23" }
  }
}

variable "environment" {
  type    = string
  default = "dev"
}
variable "aws_region" {
  type    = string
  default = "us-east-1"
}
variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Pass the selected AWS account ID."
  }
}
variable "settings" {
  description = "Explicit Cyber hosted-worker settings"
  type        = map(string)
  default     = {}
}
variable "images" {
  description = "Optional digest-pinned Cyber browser image"
  type        = map(string)
  default     = {}
  validation {
    condition = (
      length(setsubtract(keys(var.images), ["cyber-browser"])) == 0 &&
      alltrue([for image in values(var.images) : can(regex("@sha256:[0-9a-f]{64}$", image))])
    )
    error_message = "Only the Cyber browser image may be supplied, at an immutable digest."
  }
}

provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = {
      Project     = "adp"
      Environment = var.environment
      Module      = "domain-apps/cyber/hosted-integration"
      DomainApp   = "cyber"
      ManagedBy   = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_eks_cluster" "platform" {
  name = "adp-${var.environment}-eks-cluster"
}
data "aws_eks_cluster_auth" "platform" {
  name = data.aws_eks_cluster.platform.name
}
provider "kubernetes" {
  host                   = data.aws_eks_cluster.platform.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.platform.certificate_authority[0].data)
  token                  = data.aws_eks_cluster_auth.platform.token
}

data "terraform_remote_state" "platform" {
  backend = "s3"
  config = {
    bucket = "adp-terraform-state-${data.aws_caller_identity.current.account_id}"
    key    = "${var.environment}/platform/terraform.tfstate"
    region = var.aws_region
  }
}
data "terraform_remote_state" "webhook" {
  backend = "s3"
  config = {
    bucket = "adp-terraform-state-${data.aws_caller_identity.current.account_id}"
    key    = "${var.environment}/modules/webhook-ingress/terraform.tfstate"
    region = var.aws_region
  }
}

resource "terraform_data" "browser_image_guard" {
  input = var.images
  lifecycle {
    precondition {
      condition = lookup(var.settings, "browser_broker_enabled", "true") != "true" || can(regex(
        "^${var.account_id}\\.dkr\\.ecr\\.${var.aws_region}\\.amazonaws\\.com/adp-cyber-browser@sha256:[0-9a-f]{64}$",
        lookup(var.images, "cyber-browser", "")
      ))
      error_message = "The Cyber browser broker requires a digest-pinned image in the selected account."
    }
  }
}

module "cyber" {
  source                  = "../platform-integration"
  name_prefix             = "adp-${var.environment}"
  aws_region              = var.aws_region
  environment             = var.environment
  account_id              = data.aws_caller_identity.current.account_id
  namespace               = data.terraform_remote_state.webhook.outputs.agent_scaledjob_namespace
  oidc_provider_arn       = data.terraform_remote_state.platform.outputs.eks_oidc_provider_arn
  oidc_issuer             = data.terraform_remote_state.platform.outputs.eks_oidc_issuer
  worker_role_name        = data.terraform_remote_state.webhook.outputs.agent_scaledjob_role_name
  broker_image            = lookup(var.images, "cyber-browser", "")
  common_crawl_partitions = compact(split(",", lookup(var.settings, "common_crawl_partitions", "")))
  task_url_tools_enabled  = lookup(var.settings, "task_url_tools_enabled", "false") == "true"
  websearch_enabled       = lookup(var.settings, "websearch_enabled", "false") == "true"
  tools_endpoint          = lookup(var.settings, "tools_endpoint", "")
  browser_mode            = lookup(var.settings, "browser_mode", "broker")
  browser_broker_enabled  = lookup(var.settings, "browser_broker_enabled", "true") == "true"
  session_owner_routing   = lookup(var.settings, "session_owner_routing", "false") == "true"
  depends_on              = [terraform_data.browser_image_guard]
}

output "worker_environment" { value = module.cyber.worker_environment }
output "worker_artifact_resources" { value = module.cyber.worker_artifact_resources }
output "worker_egress" { value = module.cyber.worker_egress }
output "worker_browser_permissions" { value = module.cyber.worker_browser_permissions }
