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
    ManagedBy   = "terraform"
    Component   = "cyber-sandbox"
    Isolation   = "required"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}
