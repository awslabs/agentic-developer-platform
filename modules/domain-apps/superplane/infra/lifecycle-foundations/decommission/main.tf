# Full installation decommissioning only, through teardown_infrastructure.py.
# Resource configuration is intentionally absent: Terraform consumes the exact
# existing foundation state and produces a saved delete-only plan. The app script
# verifies every actor/key identity and owner tag before plan and apply, and refuses
# application while workspace state remains. This also handles an already removed
# provisioning role without inventing or recreating that role to delete its former
# foundations. This root must never be used for workspace retirement or provisioning.
terraform {
  required_version = ">= 1.9.8, < 2.0.0"
  backend "s3" {}
  required_providers {
    aws = { source = "hashicorp/aws", version = "= 6.65.0" }
  }
}

provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.account_id]
}
