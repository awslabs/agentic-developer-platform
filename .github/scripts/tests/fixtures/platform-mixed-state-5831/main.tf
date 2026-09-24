# Reproduction configuration for issue #5831 — see this directory's README.
#
# Deliberately mirrors the platform root's *shape*, not its content: one
# resource whose state record is older than the provider's schema, one that is
# in scope for a targeted plan, and NO declaration of the aws_eks_addon the
# state record carries. That last omission is not an oversight — it is the
# real, observed state/source mismatch this fixture reproduces.
#
# The AWS provider constraint is intentionally injected by the test rather than
# written here, because the whole point is to compare provider versions against
# one identical state record.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "AWS_VERSION_CONSTRAINT" # substituted by the test
    }
  }
}

provider "aws" {
  region = "us-east-1"

  # No credentials are needed or used: every plan in this reproduction runs with
  # -refresh=false, or as -refresh-only against a state record that is never
  # written back. These stop the provider looking for credentials it will not get.
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  access_key                  = "mock_access_key"
  secret_key                  = "mock_secret_key"
}

# In scope for the targeted plan below. Stands in for the real platform's
# module.eks.aws_eks_cluster.main.
resource "aws_eks_cluster" "main" {
  name     = "adp-dev"
  role_arn = "arn:aws:iam::000000000000:role/placeholder"

  vpc_config {
    subnet_ids = ["subnet-aaaa", "subnet-bbbb"]
  }
}

# NOT in scope for the targeted plan, and its state record predates the
# provider's current schema version. This is the resource the real targeted
# plan's JSON export fails on: targeting excludes it, so its state schema is
# never upgraded, yet the export still has to serialise it.
resource "aws_launch_template" "gvisor_nodes" {
  name_prefix   = "adp-dev-gvisor-"
  instance_type = "m6i.large"
}
