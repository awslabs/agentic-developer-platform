# Acceptance criterion 3, first half: no inherited upstream account default.
# Issue #5042 (U3), EPIC #4910.
#
# THE RULE IS PROVENANCE, NOT VALUE CLASS.
#
# An account id is not a secret — design §7 line 502: "Account IDs are non-secret metadata
# but still access-controlled" — and an account explicitly selected for a deployment is a
# legitimate tfvars input. So a test that rejected "any 12-digit number" would be testing
# the wrong thing and would block the module's own required input.
#
# What is forbidden is INHERITING the snapshot's accounts as a DEFAULT. This file asserts
# both directions of that distinction, because either one alone is satisfiable by a wrong
# implementation:
#
#   * reject-only would pass a module that also rejected ADP's own account;
#   * accept-only would pass a module that inherited upstream's.
#
# WHY THE DENY LIST HAS TWO ENTRIES AND NOT THREE
#
# The planning analysis counts three hardcoded account IDs in the snapshot. Those are
# three SITES over two VALUES: 938500344975 appears twice (an EKS cluster ARN in
# deploy/db-seed-job.yaml, and an OrganizationAccountAccessRole ARN in
# infra/skypilot-api/README.md). There is no third distinct value to block, and inventing
# one to match the prose would be worse than being explicit about the count.
#
# A third value was checked and deliberately NOT blocked — see
# `adp_own_beads_account_is_not_blocked` below. That run is the one that makes this file a
# provenance test rather than a blocklist.

# The mocked provider invents a random string for every computed attribute, including
# `aws_iam_policy_document.json` — and `aws_iam_role` validates that attribute as JSON, so
# the plan fails on "not a JSON object" before reaching any assertion. Supplying a valid
# empty policy as the mock default fixes that without weakening anything these tests check:
# `statement` is a CONFIGURED block, so it survives mocking and the assertions below still
# read the real policy the module declares.
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

# File-level overrides, applied to every run below.
#
# `data.terraform_remote_state.platform` is not an AWS data source, so `mock_provider
# "aws"` does not cover it: unmocked, it makes a real S3 call and the whole file fails with
# NoSuchBucket. Overriding it here rather than inside each run also keeps the negative
# runs honest — a run whose point is `expect_failures` must not need a body of unrelated
# scaffolding to express that.
#
# These are plan-only fixtures. They prove what the configuration DECLARES about the
# platform interface; they cannot prove the platform actually exports these outputs. That
# is a live check and is deferred with the rest of R3 acc. 2.
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

# ---------------------------------------------------------------------------
# The required-input half: there is no default to inherit.
#
# `account_id` has no default in variables.tf. This run does not attempt to prove absence
# by omitting it (terraform test would fail with "no value for required variable", which
# is a framework error rather than an assertion). Instead the companion Python guard
# (tests/test_no_inherited_defaults.py) parses variables.tf and asserts the block declares
# no default at all — the two together cover both the shape and the behaviour.
# ---------------------------------------------------------------------------
run "a_deliberately_supplied_account_is_accepted" {
  command = plan

  # The positive half of acceptance criterion 3: a supplied account is an INPUT, and the
  # module plans with it rather than refusing it for being an account id.
  assert {
    condition     = var.account_id == "111122223333"
    error_message = "a deliberately supplied account id must be accepted as a legitimate input (provenance, not value class)."
  }

  # And it is the account the resources are actually scoped to, not just a variable that
  # was accepted and ignored.
  assert {
    condition = alltrue([
      for arn in data.aws_iam_policy_document.control_plane.statement[0].resources :
      strcontains(arn, "111122223333")
    ])
    error_message = "the supplied account id must be the one the module scopes its secret ARNs to."
  }
}

# ---------------------------------------------------------------------------
# The rejection half. One run per upstream account, so a failure names which one.
# ---------------------------------------------------------------------------
run "upstream_ecr_and_state_account_is_rejected" {
  command = plan

  variables {
    # Upstream's ECR registry and Terraform state bucket account. In the snapshot:
    # ci-controller.yml, ci-platform-monitor.yml, infra/skypilot-api/config.env
    # (AWS_ACCOUNT_ID), and versions.tf's hardcoded
    # bucket = "superplane-terraform-state-605440105851" with no variable to override it.
    account_id = "605440105851"
  }

  expect_failures = [var.account_id]
}

run "upstream_test_cluster_account_is_rejected" {
  command = plan

  variables {
    # Upstream's test cluster account — the value behind two of the three hardcoded
    # sites: an EKS cluster ARN in deploy/db-seed-job.yaml and an
    # OrganizationAccountAccessRole ARN in infra/skypilot-api/README.md.
    account_id = "938500344975"
  }

  expect_failures = [var.account_id]
}

# ---------------------------------------------------------------------------
# THE RUN THAT MAKES THIS A PROVENANCE TEST.
#
# 193832579677 appears in the upstream snapshot (reference/tmp/agent-*.yml) and so looks
# like a third upstream account. It is not: those files are copies of ADP's OWN workflows,
# and the literal is live on main today in .github/workflows/agent-developer.yml and eight
# siblings as `${{ vars.BEADS_S3_BUCKET || 'adp-beads-state-193832579677' }}`.
#
# Blocking it would reject a legitimate ADP account for resembling snapshot material —
# value-class reasoning, which is the error acceptance criterion 3 is written against.
# This run fails if someone "hardens" the deny list by adding it.
# ---------------------------------------------------------------------------
run "adp_own_beads_account_is_not_blocked" {
  command = plan

  variables {
    account_id = "193832579677"
  }

  assert {
    condition     = var.account_id == "193832579677"
    error_message = "193832579677 is ADP's own account (live in .github/workflows/agent-*.yml on main), not an upstream account. It must not be added to the deny list — presence in the snapshot is not the test; ownership is."
  }
}

# ---------------------------------------------------------------------------
# A malformed account id is a different failure from a forbidden one, and both must fail.
# ---------------------------------------------------------------------------
run "a_non_account_id_is_rejected" {
  command = plan

  variables {
    account_id = "not-an-account"
  }

  expect_failures = [var.account_id]
}

# ---------------------------------------------------------------------------
# The committed tfvars ship `account_id = "ACCOUNT_ID"`, a placeholder that
# `platform/scripts/bootstrap.sh` rewrites with the account the operator is actually
# authenticated to. This run pins the behaviour when that substitution has NOT happened.
#
# It must fail, and it must fail HERE — at variable validation, before any resource is
# planned. The alternative is the failure mode that makes placeholders dangerous: a
# not-quite-valid value that flows far enough into a plan to create something misnamed, or
# to be reported as an inscrutable AWS API error a long way from its cause.
#
# Verified by running it: an unsubstituted placeholder stops the plan with "account_id must
# be a 12-digit AWS account id."
# ---------------------------------------------------------------------------
run "an_unsubstituted_account_placeholder_is_rejected" {
  command = plan

  variables {
    account_id = "ACCOUNT_ID"
  }

  expect_failures = [var.account_id]
}
