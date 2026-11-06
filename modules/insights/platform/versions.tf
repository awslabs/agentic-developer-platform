terraform {
  required_version = ">= 1.7, < 2.0"
  required_providers {
    aws        = { source = "hashicorp/aws", version = "~> 5.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.23" }
  }
  backend "s3" {}
}

provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.aws_account_id]
}

provider "kubernetes" {
  host                   = data.terraform_remote_state.platform.outputs.eks_cluster_endpoint
  cluster_ca_certificate = base64decode(data.terraform_remote_state.platform.outputs.eks_cluster_ca_certificate)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", data.terraform_remote_state.platform.outputs.eks_cluster_name, "--region", var.aws_region]
  }
}

variable "environment" {
  type = string
  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "Only dev and prod are supported."
  }
}
variable "aws_region" { type = string }
variable "aws_account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "Supply the verified AWS account ID."
  }
}
variable "emitter_image" {
  type    = string
  default = ""
  validation {
    condition     = var.emitter_image == "" || can(regex("@sha256:[a-f0-9]{64}$", var.emitter_image))
    error_message = "Emitter image must use an immutable SHA-256 digest."
  }
}
variable "deployment_role_boundaries" {
  type        = map(string)
  default     = {}
  nullable    = false
  description = "The two exact Insights role-to-boundary entries approved in the operator-owned automation workload inventory."
  validation {
    condition = alltrue([for role, boundary in var.deployment_role_boundaries :
      can(regex("^arn:aws:iam::[0-9]{12}:role/adp-(dev|prod)-insights-(emitter|distiller)$", role)) &&
      can(regex("^arn:aws:iam::[0-9]{12}:policy/[A-Za-z0-9_/-]+$", boundary))
    ])
    error_message = "Supply exact Insights role ARNs and non-null operator-approved boundary policy ARNs, without wildcards."
  }
}
variable "distiller_image" {
  type    = string
  default = ""
  validation {
    condition     = var.distiller_image == "" || can(regex("@sha256:[a-f0-9]{64}$", var.distiller_image))
    error_message = "Distiller image must use an immutable SHA-256 digest."
  }
}
variable "github_api_cidrs" {
  type    = list(string)
  default = []
  validation {
    condition     = alltrue([for cidr in var.github_api_cidrs : can(cidrhost(cidr, 0)) && cidr != "0.0.0.0/0"])
    error_message = "Provide specific GitHub API egress CIDRs; unrestricted egress is forbidden."
  }
}

variable "l5_consumer_verification_keys" {
  type        = map(string)
  default     = {}
  description = "Gateway public Ed25519 verification keys only; never private signing material."
  validation {
    condition = alltrue([for key in values(var.l5_consumer_verification_keys) :
      startswith(key, "-----BEGIN PUBLIC KEY-----") && !strcontains(key, "PRIVATE KEY")
    ])
    error_message = "Only public verification keys may be distributed to Insights."
  }
}

variable "l5_authority_gateway_url" {
  type        = string
  default     = "http://bedrockgateway.adp-gateway.svc.cluster.local:80"
  description = "Private Gateway authority callback; network policy permits only Gateway pods."
  validation {
    condition     = can(regex("^https?://[a-z0-9.-]+(:[0-9]+)?$", var.l5_authority_gateway_url))
    error_message = "Provide a private origin without credentials, query, fragment, or path."
  }
}

data "terraform_remote_state" "platform" {
  backend = "s3"
  config = {
    bucket = "adp-terraform-state-${var.aws_account_id}"
    key    = "${var.environment}/platform/terraform.tfstate"
    region = var.aws_region
  }
}

data "aws_prefix_list" "s3" {
  name = "com.amazonaws.${var.aws_region}.s3"
}

data "aws_vpc_endpoint" "sts" {
  vpc_id       = data.terraform_remote_state.platform.outputs.vpc_id
  service_name = "com.amazonaws.${var.aws_region}.sts"

  lifecycle {
    postcondition {
      condition     = self.vpc_endpoint_type == "Interface" && self.private_dns_enabled && self.state == "available" && length(self.network_interface_ids) > 0
      error_message = "Insights requires the platform's available private-DNS STS interface endpoint with network interfaces."
    }
  }
}

data "aws_network_interface" "sts" {
  for_each = toset(data.aws_vpc_endpoint.sts.network_interface_ids)
  id       = each.value
}

locals {
  prefix      = "adp-${var.environment}-insights"
  oidc_issuer = data.terraform_remote_state.platform.outputs.eks_oidc_issuer
  oidc_arn    = data.terraform_remote_state.platform.outputs.eks_oidc_provider_arn
  images      = { for name, image in { emitter = var.emitter_image, distiller = var.distiller_image } : name => image if image != "" }
}
