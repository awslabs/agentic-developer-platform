# Terraform state backend for the Superplane control plane — Issue #5042 (U3), EPIC #4910.
#
# Per-environment by construction. The key embeds the environment name, so `dev` and any
# later environment address different objects in the same bucket and a `terraform apply`
# in one can never plan a destroy against the other's resources. That is acceptance
# criterion 1 of R3, and `tests/backend.tftest.hcl` asserts the shape below.
#
# ACCOUNT_ID is a placeholder, not a value. The Superplane workflows substitute the
# authenticated caller's account into this app-owned backend configuration. Core platform
# bootstrap does not rewrite it. Direct operators must prepare a private target-bound copy.
# Moving this file does not move the state object: the key below deliberately remains the
# deployed dev/modules/superplane/terraform.tfstate key. No state migration is implied.
bucket         = "adp-terraform-state-ACCOUNT_ID"
key            = "dev/modules/superplane/terraform.tfstate"
region         = "us-east-1"
encrypt        = true
dynamodb_table = "adp-terraform-locks"
