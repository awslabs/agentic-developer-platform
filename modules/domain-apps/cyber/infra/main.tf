# =============================================================================
# Cyber Sandbox Infrastructure — CAPE Malware Analysis
# =============================================================================
# Issue #225: Threat Research VPC + CAPE EC2 host + VPC peering to ADP VPC.
#
# This module is standalone — it does NOT depend on the shared platform
# Terraform state for its core resources. It references the ADP VPC only
# in the peering subresource (Phase 6).
#
# All resources are tagged with Component=cyber-sandbox, Isolation=required.
# =============================================================================

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

# ---------------------------------------------------------------------------
# Remote state: ADP platform (VPC, EKS, networking outputs)
# Used by peering.tf to resolve ADP VPC ID, route tables, and security groups
# without hardcoding live infrastructure IDs in tfvars.
# ---------------------------------------------------------------------------
data "terraform_remote_state" "platform" {
  backend = "s3"
  config = {
    bucket = "adp-terraform-state-${data.aws_caller_identity.current.account_id}"
    key    = "${var.environment}/platform/terraform.tfstate"
    region = var.aws_region
  }
}

locals {
  name_prefix = "adp-${var.environment}-cyber"

  common_tags = {
    Project     = "adp"
    Environment = var.environment
    Module      = "domain-apps/cyber"
    ManagedBy   = "terraform"
    Owner       = "agent-team"
    CostCenter  = "engineering"
    Component   = "cyber-sandbox"
    DomainApp   = "cyber"
    Isolation   = "required"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}

# Cyber image builders are installed from the Cyber module's own state.
module "image_builds" {
  source                     = "../../shared/infra/codebuild-projects"
  domain_app                 = "cyber"
  projects                   = jsondecode(file("${path.module}/../codebuild/projects.json"))
  name_prefix                = "adp-${var.environment}"
  account_id                 = data.aws_caller_identity.current.account_id
  aws_region                 = var.aws_region
  state_bucket               = "adp-terraform-state-${data.aws_caller_identity.current.account_id}"
  security_scans_bucket_name = data.terraform_remote_state.platform.outputs.security_scans_bucket_name
  permissions_boundary_arn   = data.terraform_remote_state.platform.outputs.codebuild_boundary_arn
  common_tags                = local.common_tags
  allowed_artifact_writes    = ["cape-assets/worker-manifests"]
}
