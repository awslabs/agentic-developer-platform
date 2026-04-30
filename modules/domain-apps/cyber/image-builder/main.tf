# =============================================================================
# Cyber Image Builder — Windows 10 CAPE-ready qcow2
# =============================================================================
# Issue #227: Build a CAPE-ready Windows 10 analysis VM disk image and upload
# to S3. This module is separate from the Track A CAPE host (issue #225).
#
# The build host is ephemeral — it spins up in an ADP dev private subnet,
# builds the qcow2 via KVM, uploads to S3, and is terminated.
#
# Usage:
#   terraform init -backend-config=<backend.tfvars>
#   terraform apply -var build_host_enabled=true   # spin up builder
#   # Wait for build to complete (~45 min), then:
#   terraform apply -var build_host_enabled=false   # tear down builder
# =============================================================================

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  name_prefix = "adp-${var.environment}-imgbuilder"

  common_tags = {
    Project     = "adp"
    Environment = var.environment
    ManagedBy   = "terraform"
    Component   = "cyber-sandbox"
    Purpose     = "windows-qcow2-build"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}

# ---------------------------------------------------------------------------
# VPC / Subnet self-discovery via tags (module independence — no remote state)
# ---------------------------------------------------------------------------

locals {
  resolved_vpc_name = var.vpc_name != "" ? var.vpc_name : "adp-${var.environment}-vpc"
}

data "aws_vpc" "target" {
  filter {
    name   = "tag:Name"
    values = [local.resolved_vpc_name]
  }
}

data "aws_subnets" "private" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.target.id]
  }

  filter {
    name   = "tag:Name"
    values = ["adp-${var.environment}-private-*"]
  }
}

locals {
  vpc_id    = data.aws_vpc.target.id
  subnet_id = var.subnet_id_override != "" ? var.subnet_id_override : data.aws_subnets.private.ids[0]
}
