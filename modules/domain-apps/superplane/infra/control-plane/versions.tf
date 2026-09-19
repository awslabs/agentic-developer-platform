terraform {
  # >= 1.9, not >= 1.5. The declared constraint must cover the features this module and
  # its test suite actually use, or a runner with a satisfying-but-too-old Terraform fails
  # in a confusing place instead of at version negotiation (PR #5283 review):
  #
  #   1.6  `terraform test` / .tftest.hcl files at all
  #   1.7  `mock_provider`, which every test here relies on to run without credentials
  #   1.9  cross-variable `validation` conditions — variables.tf's CORS rule refers to
  #        var.cors_allow_credentials from inside var.cors_allowed_origins' validation
  required_version = ">= 1.9"

  backend "s3" {
    # Configured via -backend-config during terraform init. Deliberately empty here:
    # a bucket or key written inline would be the same environment-pinning defect that
    # environments/dev/modules/superplane-backend.tfvars exists to avoid, and would make
    # the "state is per-environment" property depend on editing this file.
    #
    # See environments/dev/modules/superplane-backend.tfvars
    #   key = "<environment>/modules/superplane/terraform.tfstate"
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}
