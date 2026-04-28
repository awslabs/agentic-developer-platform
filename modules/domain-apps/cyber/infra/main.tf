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
