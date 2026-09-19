# Terraform state backend for the Superplane control plane — Issue #5042 (U3), EPIC #4910.
#
# Per-environment by construction. The key embeds the environment name, so `dev` and any
# later environment address different objects in the same bucket and a `terraform apply`
# in one can never plan a destroy against the other's resources. That is acceptance
# criterion 1 of R3, and `tests/backend.tftest.hcl` asserts the shape below.
#
# ACCOUNT_ID is a placeholder, not a value. `platform/scripts/bootstrap.sh` and
# `deploy-all.sh` substitute the account the operator is actually authenticated to
# (deploy-all.sh writes this file from ${STATE_BUCKET} if it is absent). Committing a
# resolved account id here is the "inherited default" defect this unit exists to prevent:
# it would silently point a deploy at whichever account was current when the file was
# written. Same placeholder convention as gateway-backend.tfvars and
# cyber-sandbox-backend.tfvars.
bucket         = "adp-terraform-state-ACCOUNT_ID"
key            = "dev/modules/superplane/terraform.tfstate"
region         = "us-east-1"
encrypt        = true
dynamodb_table = "adp-terraform-locks"
