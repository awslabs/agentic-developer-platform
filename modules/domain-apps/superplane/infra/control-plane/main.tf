# =============================================================================
# Superplane control plane — ADP-owned deployment wrapper
# Issue #5042 (U3), EPIC #4910.
# =============================================================================
# WHAT THIS MODULE IS
#
# The ADP-owned wrapper that deploys the pinned Superplane control plane and the
# SkyPilot API service into an ADP account. It is a *wrapper*: the application code
# and its migrations stay upstream-owned (releases/superplane.lock.yaml pins what
# runs), and this module owns only the AWS-side surface those images need.
#
# WHAT IT DELIBERATELY DOES NOT OWN
#
# Kubernetes objects are not declared here. They are applied by
# .github/workflows/superplane-k8s-deploy.yml from ../../k8s/, mirroring the
# cyber-infra / cyber-k8s-deploy split this unit was told to copy. The reason is
# operational rather than stylistic: a `kubernetes` provider in this module would make
# every `terraform plan` — including the plan lane, which has no cluster — require EKS
# API reachability, and would put a rollout of application pods and the creation of IAM
# roles behind the same apply.
#
# PLATFORM ISOLATION (confirmed requirement, 2026-09-16)
#
# This module CONSUMES platform outputs read-only through the remote state data source
# below, and OWNS nothing the platform owns. Every resource it declares is named
# `adp-<env>-superplane-*` and lives in this module's own state
# (`<environment>/modules/superplane/terraform.tfstate`). Destroying this module cannot
# reach a platform resource, because it never took one into its state — a separate state
# key alone would not establish that, so `tests/platform_isolation.tftest.hcl` asserts
# the resource set directly.
#
# DECISION 2 (DATABASE HOSTING) IS UNRESOLVED — READ BEFORE ADDING AN RDS RESOURCE
#
# This module provisions NO database. Whether the Superplane schema lives on a shared
# instance or a separate one is an unresolved product decision, and provisioning either
# shape here would decide it by default. So the database arrives as a *reference*
# (var.database_secret_name — a Secrets Manager secret the operator has already seeded),
# and this module makes no isolation, backup, retention or restore claim about it. When
# Decision 2 is made, whoever makes it adds the resource or the shared-instance lookup;
# nothing here has to be unwound first.
# =============================================================================

data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# Platform interface (read-only)
#
# The approved way to learn the cluster's identity without hardcoding live
# infrastructure ids in tfvars, and the same mechanism cyber/infra/main.tf uses. This is
# a *read*: the platform state is never written, and no platform resource enters this
# module's state.
#
# The bucket is derived from the caller's own account rather than a variable, so this
# module cannot be pointed at another account's platform state by a tfvars edit.
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
  # Every resource this module creates carries this prefix. Scoped naming is half of
  # what makes the isolation claim checkable: a resource without it is either not ours
  # or is a resource we should not be creating.
  name_prefix = "adp-${var.environment}-superplane"

  oidc_provider_arn = data.terraform_remote_state.platform.outputs.eks_oidc_provider_arn
  oidc_issuer       = data.terraform_remote_state.platform.outputs.eks_oidc_issuer

  common_tags = {
    Project     = "adp"
    Environment = var.environment
    Module      = "domain-apps/superplane"
    ManagedBy   = "terraform"
    Component   = "superplane-control-plane"
    # Marks this as an optional domain app whose absence must not affect core ADP.
    # Used by cost attribution and by teardown to identify domain-owned resources.
    DomainApp = "superplane"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}
