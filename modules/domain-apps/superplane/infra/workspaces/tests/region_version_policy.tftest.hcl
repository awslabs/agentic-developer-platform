# =============================================================================
# Unavailable regions and versions are refused BEFORE anything is created — #5532, finding F6.
# =============================================================================
# F6: "regex-only region/version checks allow unavailable and nonexistent targets. Implement a
# reviewed versioned region/version support policy (and support-tier price consistency),
# checking it before mutations. Exercise retired, future and unavailable combinations offline."
#
# WHY THE REGEX WAS NOT ENOUGH
#
# The two conditions this replaces were:
#
#     can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]$", var.aws_region))
#     can(regex("^1\\.(2[5-9]|3[0-9])$", var.cluster_version))
#
# A pattern describes the SHAPE of an identifier. Whether a region exists, whether this account
# may use it, and whether EKS will still create a given Kubernetes version are facts about the
# world on a date, and no pattern can express them. So `xx-fake-1`, `us-east-2`, `1.25` and
# `1.39` all passed validation and failed at APPLY — after the VPC, its subnets and its NAT
# gateway existed, leaving a partial workspace to clean up by hand and an opaque AWS error to
# read.
#
# WHERE "BEFORE MUTATIONS" IS ACTUALLY PROVEN
#
# Here, and it is the reason this file exists alongside the Python tests. Terraform evaluates
# variable validations before it evaluates any resource, so a run that fails at
# `var.cluster_version` produced NO plan at all — not a plan that was refused downstream. The
# Python suite (`tests/test_region_version_policy.py`) proves the policy's contents are right
# and that both halves agree; only a `terraform test` run proves the refusal happens at the
# variable. Neither is sufficient alone.
#
# WHAT `expect_failures` PROVES, AND WHAT IT DOES NOT
#
# It proves the named variable rejected the value. It does NOT say which of that variable's
# validations fired — both `aws_region` and `cluster_version` now have two — so every run below
# changes exactly ONE field of the known-good baseline, and the shape check and the membership
# check are exercised with values that can only trip one of them:
#
#   * a shape-check value is malformed ("latest", "1.33.2") and never reaches membership;
#   * a membership-check value is WELL-FORMED and real ("us-east-2", "1.30"), so the shape
#     check passes and only the allowlist can refuse it.
#
# That distinction is the whole finding. A value that failed both would pass this file while
# proving nothing about the part that was missing.
#
# THE POSITIVE HALF
#
# A module that refused every region and every version would satisfy every negative run here.
# `a_reviewed_region_and_a_standard_support_version_plan_successfully` and the extended-support
# run are the other side: they must plan, or the policy is not a policy but an outage.
#
# MEASURED AGAINST THE REVIEWED HEAD, NOT ASSUMED
#
# Run against 2f75e700's regex-only validations (restored into a copy of this module; Terraform
# aborts a file at its first failure, so each run was extracted into its own file to get a
# per-run verdict rather than one failure and eleven skips):
#
#   xx-fake-1                              Missing expected failure  <- accepted
#   us-east-2                              Missing expected failure  <- accepted
#   ap-east-1                              Missing expected failure  <- accepted
#   cn-north-1                             Missing expected failure  <- accepted
#   1.25 (retired)                         Missing expected failure  <- accepted
#   1.30 (retired)                         Missing expected failure  <- accepted
#   1.39 (does not exist)                  Missing expected failure  <- accepted
#   US_East_1 (malformed)                  passed                    <- shape check, pre-existing
#   "latest"                               passed                    <- shape check, pre-existing
#   "1.33.2"                               passed                    <- shape check, pre-existing
#   both positive runs                     passed
#
# Seven reproductions, and the split is exactly the finding: every value that was WELL-FORMED
# and unavailable was accepted, and every value that was MALFORMED was already refused. The
# three pre-existing passes are not padding — they are what stops the shape checks from becoming
# dead code now that an allowlist sits behind them, and they would have failed had this repair
# replaced the pattern instead of adding to it.
# =============================================================================

mock_provider "aws" {
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
  cluster_version = "1.33"

  networking_mode    = "owned"
  vpc_cidr           = "10.64.0.0/16"
  availability_zones = ["us-east-1a", "us-east-1b"]
}

# ---------------------------------------------------------------------------
# REGIONS — F6's "unavailable" case
#
# Each value below is WELL-FORMED, so the shape validation passes and only the reviewed
# allowlist can refuse it. That is deliberate: a malformed region would fail the shape check
# and these runs would pass without the allowlist existing at all.
# ---------------------------------------------------------------------------
run "a_nonexistent_region_that_matches_the_region_pattern_is_refused" {
  command = plan

  variables {
    # The exact value the regex accepted. Two lowercase letters, a word, a digit — every shape
    # rule for an AWS region identifier, and not a region.
    aws_region = "xx-fake-1"
  }

  expect_failures = [var.aws_region]
}

run "a_real_but_unreviewed_region_is_refused" {
  command = plan

  variables {
    # us-east-2 exists and works. It is still refused, because "this region is real" and "this
    # platform is reviewed to put tenant data there and can price it" are different claims, and
    # only a regex confuses them. No tenant has a residency requirement for it, and it has no
    # entry in the estimate's rate multipliers — so a workspace here could not be bounded.
    aws_region = "us-east-2"
  }

  expect_failures = [var.aws_region]
}

run "an_opt_in_region_is_refused_because_the_plan_cannot_see_whether_it_is_enabled" {
  command = plan

  variables {
    # The strongest case for an allowlist over a pattern. Whether ap-east-1 is usable depends on
    # ACCOUNT STATE — has the account opted in — which is not a property of the identifier and
    # not visible from a plan. No validation of the string can establish it, so the region is
    # refused rather than attempted.
    aws_region = "ap-east-1"
  }

  expect_failures = [var.aws_region]
}

run "another_partition_is_refused" {
  command = plan

  variables {
    # aws-cn is a different partition: every ARN this module builds changes prefix, and the
    # account is a separate commercial relationship. Shape-identical to a commercial region.
    aws_region = "cn-north-1"
  }

  expect_failures = [var.aws_region]
}

run "a_malformed_region_is_refused_by_the_shape_check" {
  command = plan

  variables {
    # The other validation on this variable. Kept so the shape check does not quietly become
    # dead code once the allowlist exists — it is what gives a typo a message about its shape
    # instead of a list of five regions to scan.
    aws_region = "US_East_1"
  }

  expect_failures = [var.aws_region]
}

# ---------------------------------------------------------------------------
# VERSIONS — F6's "retired" and "future" cases
# ---------------------------------------------------------------------------
run "a_retired_version_is_refused" {
  command = plan

  variables {
    # 1.25 matched the pattern this replaced. EKS standard support for it ended 2024-05-01 and
    # it is no longer createable. Well-formed, so only the allowlist can refuse it.
    cluster_version = "1.25"
  }

  expect_failures = [var.cluster_version]
}

run "the_most_recently_retired_version_is_refused_too" {
  command = plan

  variables {
    # 1.30 is the boundary case and the one that matters in practice: recent enough to still be
    # in someone's notes, retired on 2025-07-23. AWS auto-upgrades clusters off retired
    # versions, so a workspace "created" at 1.30 is a workspace whose version is not the one
    # that was reviewed — the pin becomes a suggestion.
    #
    # This value is not hypothetical. `no_inherited_defaults.tftest.hcl` pinned 1.30 until this
    # policy landed, and the allowlist failed that run the first time it executed. That was the
    # control working: the finding arriving at `terraform test` rather than at apply.
    cluster_version = "1.30"
  }

  expect_failures = [var.cluster_version]
}

run "a_version_that_does_not_exist_yet_is_refused" {
  command = plan

  variables {
    # F6's "future" case, and the one a range check cannot catch: 1.39 sits inside the old
    # pattern's 1.25–1.39 window and EKS does not offer it. An apply would fail after the
    # network was built.
    cluster_version = "1.39"
  }

  expect_failures = [var.cluster_version]
}

run "a_moving_label_is_refused_by_the_shape_check_not_the_allowlist" {
  command = plan

  variables {
    # Malformed, so it fails the pattern before membership is considered. Kept here because the
    # two validations now overlap in coverage, and the pin rule (AC-02's "no latest image") must
    # not come to depend on a version allowlist that a future reviewer might widen.
    cluster_version = "latest"
  }

  expect_failures = [var.cluster_version]
}

run "a_patch_level_version_is_refused" {
  command = plan

  variables {
    # 1.33 is supported; "1.33.2" is not the kind of thing EKS's version field takes. Shape
    # check again — and worth pinning, because it would otherwise be tempting to read the
    # allowlist as the only version rule.
    cluster_version = "1.33.2"
  }

  expect_failures = [var.cluster_version]
}

# ---------------------------------------------------------------------------
# THE POSITIVE HALF — the policy must still permit the platform to be deployed
# ---------------------------------------------------------------------------
run "a_reviewed_region_and_a_standard_support_version_plan_successfully" {
  command = plan

  override_data {
    target = data.aws_availability_zones.selected
    values = { names = ["eu-central-1a", "eu-central-1b"] }
  }
  variables {
    aws_region         = "eu-central-1"
    availability_zones = ["eu-central-1a", "eu-central-1b"]
    cluster_version    = "1.34"
  }

  assert {
    condition     = aws_eks_cluster.workspace.version == "1.34"
    error_message = "A reviewed region and a standard-support version must plan, and the version must reach the cluster unchanged — no resolution, no normalisation. Got: ${aws_eks_cluster.workspace.version}"
  }
}

run "an_extended_support_version_is_permitted_deliberately" {
  command = plan

  variables {
    # Not an oversight in the allowlist. A tenant mid-upgrade has a legitimate reason to be on
    # an extended-support version, and refusing it would block a real migration. What makes
    # allowing it safe is that the plan guard prices the control plane at the extended rate
    # ($0.60/hour against $0.10), so the cost of staying is visible in the estimate rather than
    # silent — see tests/test_plan_safety.py's extended-support pricing control.
    cluster_version = "1.31"
  }

  assert {
    condition     = aws_eks_cluster.workspace.version == "1.31"
    error_message = "Extended-support versions are accepted deliberately, priced at 6x rather than refused. Got: ${aws_eks_cluster.workspace.version}"
  }
}

run "owned_zone_from_another_region_is_refused" {
  command = plan
  variables { availability_zones = ["us-east-1a", "us-west-2b"] }
  expect_failures = [terraform_data.topology_guard]
}
run "owned_unavailable_zone_is_refused" {
  command = plan
  variables { availability_zones = ["us-east-1a", "us-east-1z"] }
  expect_failures = [terraform_data.topology_guard]
}
