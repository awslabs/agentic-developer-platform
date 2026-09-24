# =============================================================================
# Nothing is inherited; the target must be named — Issue #5532 (w6-09), AC-01/AC-02.
# =============================================================================
# AC-02: "No default account, latest image, implicit core apply or shared-state ownership
# appears in rendered execution inputs."
#
# Three of those four are decided in variables.tf, by the ABSENCE of a `default` and by
# validations that refuse a value even when supplied deliberately. This file is where that
# absence becomes a checked property rather than an authoring convention.
#
# WHAT `expect_failures` PROVES, AND WHAT IT DOES NOT
#
# It proves the named variable's validation rejected the value. It does NOT prove which of
# that variable's several validations fired — so each run below changes exactly one input from
# a known-good baseline, and the run names say which rule is under test. A run that changed
# two inputs could pass for the wrong reason.
#
# The positive half matters as much as the negative half: a module that rejected EVERYTHING
# would pass every `expect_failures` run here. `backend.tftest.hcl` and
# `supplied_networking.tftest.hcl` are that half — they plan successfully from the same
# baseline.
# =============================================================================

mock_provider "aws" {
  mock_data "aws_vpc_security_group_rules" {
    defaults = { ids = ["sgr-0123456789abcdef0"] }
  }

  mock_data "aws_vpc_endpoint" {
    defaults = {
      id                  = "vpce-0123456789abcdef0"
      private_dns_enabled = true
      security_group_ids  = ["sg-0123456789abcdef0"]
    }
  }

  mock_data "aws_vpc" {
    defaults = { enable_dns_support = true, enable_dns_hostnames = true }
  }

  mock_data "aws_route_tables" {
    defaults = { ids = ["rtb-0123456789abcdef0"] }
  }
  mock_data "aws_route_table" {
    defaults = {
      id     = "rtb-0123456789abcdef0"
      vpc_id = "vpc-0a1b2c3d4e5f67890"
      routes = [{ cidr_block = "0.0.0.0/0", nat_gateway_id = "nat-0123456789abcdef0", gateway_id = "" }]
    }
  }
  mock_data "aws_nat_gateway" {
    defaults = {
      id                = "nat-0123456789abcdef0"
      vpc_id            = "vpc-0a1b2c3d4e5f67890"
      subnet_id         = "subnet-0ccccccccccccccc3"
      connectivity_type = "public"
      state             = "available"
    }
  }
  mock_data "aws_internet_gateway" {
    defaults = {
      id          = "igw-0123456789abcdef0"
      attachments = [{ vpc_id = "vpc-0a1b2c3d4e5f67890", state = "available" }]
    }
  }

  mock_data "aws_iam_session_context" {
    defaults = { issuer_arn = "arn:aws:iam::111122223333:role/workspace-provisioner" }
  }

  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b", "us-east-1c"] }
  }

  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

mock_provider "tls" {
  mock_data "tls_certificate" {
    defaults = {
      certificates = [
        {
          sha1_fingerprint = "9e99a48a9960b14926bb7f3b02e22da2b0ab7280"
        }
      ]
    }
  }
}

override_data {
  target = data.aws_caller_identity.current
  values = {
    account_id = "111122223333"
    arn        = "arn:aws:sts::111122223333:assumed-role/workspace-provisioner/test"
  }
}

override_data {
  target = data.aws_partition.current
  values = {
    partition = "aws"
  }
}

# The known-good baseline. Every run below overrides ONE field of it.
variables {
  org_id          = "test-org"
  environment     = "dev"
  workspace_name  = "tenant-alpha"
  workspace_id    = "tenant-alpha"
  account_id      = "111122223333"
  aws_region      = "us-east-1"
  cluster_version = "1.31"

  networking_mode    = "owned"
  vpc_cidr           = "10.64.0.0/16"
  availability_zones = ["us-east-1a", "us-east-1b"]
}

# ---------------------------------------------------------------------------
# THE ACCOUNT
# ---------------------------------------------------------------------------
run "an_unsubstituted_account_placeholder_is_refused" {
  command = plan

  variables {
    # The ADP tfvars convention is a literal ACCOUNT_ID replaced by sed at deploy time. When
    # that substitution does not happen the placeholder reaches Terraform, and the failure
    # must be "this is not an account id" rather than a plan against something unintended.
    account_id = "ACCOUNT_ID"
  }

  expect_failures = [var.account_id]
}

run "an_upstream_snapshot_account_is_refused_even_when_supplied_deliberately" {
  command = plan

  variables {
    # Un-defaulting alone would leave this working if pasted back in, which is why #5530
    # refuses the legacy values BY VALUE as well. 605440105851 is upstream's ECR/state
    # account and the reference Account Factory's management account.
    account_id = "605440105851"
  }

  expect_failures = [var.account_id]
}

run "the_other_upstream_snapshot_account_is_refused" {
  command = plan

  variables {
    account_id = "938500344975"
  }

  expect_failures = [var.account_id]
}

run "a_deliberately_chosen_account_is_accepted" {
  command = plan

  variables {
    # The rule is PROVENANCE, not value class. An account id is not a secret, and an account
    # deliberately selected for a workspace is a legitimate input — what is forbidden is
    # inheriting one. A module that refused all accounts would pass every negative run above
    # while being unusable.
    account_id = "444455556666"
  }

  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "444455556666"
      arn        = "arn:aws:sts::444455556666:assumed-role/workspace-provisioner/test"
    }
  }

  assert {
    condition     = output.account_id == "444455556666"
    error_message = "A deliberately supplied account must be accepted: the rule is provenance, not value class."
  }
}

run "a_named_account_that_disagrees_with_the_credentials_is_refused" {
  command = plan

  variables {
    # The target-mismatch case AC-01 names. var.account_id is required, but on its own it is
    # only a claim: nothing stops a plan naming account A while the credentials belong to
    # account B, and the apply would then build this workspace in B. main.tf's precondition
    # compares the claim against the caller's real identity.
    #
    # The baseline caller identity is 111122223333; this names a different valid account.
    account_id = "444455556666"
  }

  expect_failures = [terraform_data.target_account_guard]
}

# ---------------------------------------------------------------------------
# THE VERSION — "no latest image" (AC-02)
# ---------------------------------------------------------------------------
run "a_moving_version_label_is_refused" {
  command = plan

  variables {
    # The reference installer resolved `releases/latest` at deploy time, so what it installed
    # depended on when it ran. A label is not a pin.
    cluster_version = "latest"
  }

  expect_failures = [var.cluster_version]
}

run "the_stable_label_is_refused_too" {
  command = plan

  variables {
    cluster_version = "stable"
  }

  expect_failures = [var.cluster_version]
}

run "an_exact_minor_version_is_accepted_and_reaches_the_cluster_unchanged" {
  command = plan

  # 1.33 rather than the 1.30 this used to pin. Finding F6's support-policy allowlist refuses
  # 1.30 — EKS standard support for it ended 2025-07-23 and AWS auto-upgrades clusters off
  # retired versions — and this run caught it the first time the allowlist ran, which is the
  # behaviour F6 asked for arriving at `terraform test` instead of at apply.
  variables {
    cluster_version = "1.33"
  }

  assert {
    condition     = aws_eks_cluster.workspace.version == "1.33"
    error_message = "The exact version supplied must reach the cluster unchanged — no resolution, no normalisation. Got: ${aws_eks_cluster.workspace.version}"
  }
}

# ---------------------------------------------------------------------------
# MODE COHERENCE — a value that cannot take effect is refused, not ignored
#
# #5530's reasoning: a value that cannot take effect is a value whose author was wrong about
# what the request does. Someone supplying a CIDR alongside an existing VPC id believes this
# module is creating a network. It is not, and one of those two beliefs is wrong.
# ---------------------------------------------------------------------------
run "owned_mode_without_a_cidr_is_refused" {
  command = plan

  variables {
    vpc_cidr = ""
  }

  expect_failures = [var.vpc_cidr]
}

run "supplied_mode_with_a_cidr_is_refused_rather_than_ignoring_the_cidr" {
  command = plan

  variables {
    networking_mode             = "supplied"
    supplied_vpc_id             = "vpc-0a1b2c3d4e5f67890"
    supplied_private_subnet_ids = ["subnet-0aaaaaaaaaaaaaaa1", "subnet-0bbbbbbbbbbbbbbb2"]
    availability_zones          = []
    # Left set from the baseline. This is the input that cannot take effect.
    vpc_cidr = "10.64.0.0/16"
  }

  expect_failures = [var.vpc_cidr]
}

run "supplied_mode_without_subnets_is_refused" {
  command = plan

  variables {
    networking_mode             = "supplied"
    supplied_vpc_id             = "vpc-0a1b2c3d4e5f67890"
    supplied_private_subnet_ids = []
    vpc_cidr                    = ""
    availability_zones          = []
  }

  expect_failures = [var.supplied_private_subnet_ids]
}

run "owned_mode_with_a_supplied_vpc_id_is_refused" {
  command = plan

  variables {
    # Expresses "create a VPC, and also here is an existing one" — an adoption the author
    # expected and this mode does not perform.
    supplied_vpc_id = "vpc-0a1b2c3d4e5f67890"
  }

  expect_failures = [var.supplied_vpc_id]
}

run "an_unsupported_networking_mode_is_refused_rather_than_interpreted" {
  command = plan

  variables {
    networking_mode = "byo"
  }

  expect_failures = [var.networking_mode]
}

run "a_single_availability_zone_is_refused" {
  command = plan

  variables {
    availability_zones = ["us-east-1a"]
  }

  expect_failures = [var.availability_zones]
}

# ---------------------------------------------------------------------------
# SHARED-STATE OWNERSHIP AND NAME COLLISION (AC-02's fourth clause)
# ---------------------------------------------------------------------------
run "a_workspace_named_after_adp_itself_is_refused" {
  command = plan

  variables {
    # Would produce resource names reading as core platform infrastructure in the console and
    # in cost reports — the exact confusion the tenant boundary exists to prevent.
    workspace_name = "platform"
    workspace_id   = "platform"
  }

  expect_failures = [var.workspace_name]
}

run "an_environment_too_long_is_refused_before_any_mutation" {
  command = plan
  variables { environment = "abcdefghijk" }
  expect_failures = [var.environment]
}

run "the_longest_name_that_still_fits_is_accepted" {
  command = plan
  variables { environment = "abcdefghij" }
  assert {
    condition     = length(aws_iam_role.cluster.name) == 64
    error_message = "The maximum environment plus immutable identity must fit the IAM limit."
  }
}

override_data {
  target = data.aws_subnet.supplied["subnet-0aaaaaaaaaaaaaaa1"]
  values = { vpc_id = "vpc-0a1b2c3d4e5f67890", availability_zone = "us-east-1a", map_public_ip_on_launch = false }
}
override_data {
  target = data.aws_subnet.supplied["subnet-0bbbbbbbbbbbbbbb2"]
  values = { vpc_id = "vpc-0a1b2c3d4e5f67890", availability_zone = "us-east-1b", map_public_ip_on_launch = false }
}

override_data {
  target = data.aws_route_table.supplied_nat["nat-0123456789abcdef0"]
  values = {
    id     = "rtb-0fedcba9876543210"
    vpc_id = "vpc-0a1b2c3d4e5f67890"
    routes = [{ cidr_block = "0.0.0.0/0", gateway_id = "igw-0123456789abcdef0", nat_gateway_id = "" }]
  }
}
