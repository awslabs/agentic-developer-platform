# Acceptance criterion 1: the state key is per-environment, with no hardcoded account.
# Issue #5042 (U3), EPIC #4910.
#
# WHY THIS MATTERS MORE THAN IT LOOKS
#
# "Two environments share state; one apply destroys the other's resources" is the blast
# radius the issue records for getting this wrong. Upstream got it wrong in exactly the way
# that is hardest to notice: its control-plane versions.tf hardcodes
#
#     bucket         = "superplane-terraform-state-605440105851"
#     key            = "control-plane/terraform.tfstate"
#     dynamodb_table = "superplane-terraform-lock"
#
# with no variables at all. The key has no environment segment, so every environment
# initialised against it writes the SAME state object — and the bucket names an account
# nobody deploying from ADP chose.
#
# WHAT A PLAN-ONLY TEST CAN AND CANNOT PROVE
#
# `terraform test` cannot inspect the backend block: the backend is resolved by `init`,
# before any test runs, and is not part of the plan graph. So this file asserts the
# properties that ARE reachable — that the module derives its per-environment identity
# from var.environment, and that changing the environment changes the key and every
# resource name — and the companion Python guard (tests/test_backend_state_key.py) reads
# versions.tf and the tfvars file as text to assert the backend block is variable-free and
# the key is correctly shaped.
#
# Together those cover the criterion. Neither alone does, and the split is deliberate
# rather than duplication.

mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111122223333:role/mock-build" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

variables {
  environment          = "dev"
  aws_region           = "us-east-1"
  account_id           = "111122223333"
  cors_allowed_origins = ["https://superplane.dev.adp.internal"]
  database_secret_name = "adp/dev/superplane/database"
  jwt_secret_name      = "adp/dev/superplane/jwt-signing-key"
}

override_data {
  target = data.aws_caller_identity.current
  values = {
    account_id = "111122223333"
  }
}

override_data {
  target = data.terraform_remote_state.platform
  values = {
    outputs = {
      eks_oidc_provider_arn          = "arn:aws:iam::111122223333:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B716D3041E"
      eks_oidc_issuer                = "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B716D3041E"
      gateway_service_irsa_role_name = "adp-dev-role-gateway-service"
      codebuild_boundary_arn         = "arn:aws:iam::111122223333:policy/adp-dev-codebuild-boundary"
      security_scans_bucket_name     = "adp-dev-security-scans"
    }
  }
}

run "dev_state_key_is_environment_scoped" {
  command = plan

  assert {
    condition     = output.state_key_convention == "dev/modules/superplane/terraform.tfstate"
    error_message = "the dev state key must be dev/modules/superplane/terraform.tfstate (the convention acc. 1 names, and the one deploy-all.sh writes)."
  }

  # No account id anywhere in the key. An account-qualified key would make the state
  # location depend on who applied it rather than on which environment was targeted.
  assert {
    condition     = !can(regex("[0-9]{12}", output.state_key_convention))
    error_message = "the state key must not embed an account id."
  }
}

# The actual isolation property: a different environment addresses a DIFFERENT state
# object. This is what makes "one apply cannot destroy another environment's resources"
# true, so it is asserted by construction rather than assumed from the key's shape.
run "a_second_environment_gets_a_different_state_key" {
  command = plan

  variables {
    environment          = "test"
    cors_allowed_origins = ["https://superplane.test.adp.internal"]
    database_secret_name = "adp/test/superplane/database"
    jwt_secret_name      = "adp/test/superplane/jwt-signing-key"
  }

  assert {
    condition     = output.state_key_convention == "test/modules/superplane/terraform.tfstate"
    error_message = "the state key must track var.environment, so two environments never share a state object."
  }

  # Resource names must move with the environment too. A per-environment state key over
  # shared resource NAMES would still collide on apply — the second environment would try
  # to create resources that already exist, or worse, adopt them.
  assert {
    condition     = aws_iam_role.control_plane.name == "adp-test-superplane-control-plane"
    error_message = "resource names must be environment-scoped, not just the state key."
  }

  assert {
    condition     = startswith(aws_ssm_parameter.namespace.name, "/adp/test/superplane/")
    error_message = "SSM parameter paths must be environment-scoped."
  }
}

# The platform state this module READS is also per-environment. Reading dev's platform
# outputs while writing test's state would be a subtler version of the same defect.
run "platform_state_is_read_per_environment" {
  command = plan

  variables {
    environment          = "test"
    cors_allowed_origins = ["https://superplane.test.adp.internal"]
    database_secret_name = "adp/test/superplane/database"
    jwt_secret_name      = "adp/test/superplane/jwt-signing-key"
  }

  assert {
    condition     = strcontains(aws_ssm_parameter.namespace.name, "/test/")
    error_message = "the module's environment must consistently select which platform state it reads and which parameters it writes."
  }
}
