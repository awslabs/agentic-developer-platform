# =============================================================================
# The admin role is reachable by the principals it names — Issue #5532 (w6-09), finding F3.
# =============================================================================
# WHAT THIS FILE COVERS THAT test_admin_trust_separation.py CANNOT
#
# That file parses iam.tf's trust STATEMENTS, because the plan cannot see them: every suite
# here mocks `aws_iam_policy_document` (it must, or each role's `assume_role_policy` is
# unresolvable and the run errors before asserting anything), and the mock replaces `json`
# with an empty statement list.
#
# What a plan CAN decide is everything around the statements, and three of those are exactly
# where finding F3's second half lived:
#
#   1. Whether the role is CREATED for an automation-only workspace. The pre-repair module
#      gated creation on the human list alone, so naming only an automation role produced no
#      role at all and a null `workspace_admin_role_arn`. The automated caller then has
#      nothing to assume, and the symptom is a missing output rather than a trust problem.
#   2. Whether the variable validations refuse the principal shapes that would make the
#      un-conditioned automation statement unsafe — an account root, a wildcard, an IAM user.
#      Those are what stand in for the MFA condition that automation cannot satisfy, so they
#      are load-bearing rather than hygiene.
#   3. Whether `workspace_admin_trust` reports the two classes separately, which is how a
#      caller debugging an AccessDenied tells "never named" from "named where MFA is
#      required".
#
# THE POSITIVE AND NEGATIVE HALVES ARE BOTH HERE ON PURPOSE
#
# A module that refused every principal list would pass all the `expect_failures` runs below.
# So four runs plan SUCCESSFULLY — automation-only, human-only, both, and neither — and assert
# what each produces. Per no_inherited_defaults.tftest.hcl's rule, each negative run changes
# exactly ONE field from the baseline, so it cannot pass because of an unrelated refusal.
# =============================================================================

mock_provider "aws" {
  mock_data "aws_iam_session_context" {
    defaults = { issuer_arn = "arn:aws:iam::111122223333:role/workspace-provisioner" }
  }

  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b", "us-east-1c"] }
  }

  # Present for the reason every suite in this module has it: without it
  # `aws_iam_role.*.assume_role_policy` is an unresolved value and the plan errors. It makes
  # the trust STATEMENTS unassertable here, which is why test_admin_trust_separation.py
  # exists; nothing in this file asserts on the mocked document.
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
# THE F3 REGRESSION CONTROL.
#
# An automation-only workspace — no human operator named — must produce a usable admin role.
# On the pre-repair module this run fails: creation was gated on the human list, so the role
# count was 0 and `workspace_admin_role_arn` was null while an automation role sat named in
# the inputs.
# ---------------------------------------------------------------------------
run "an_automation_only_workspace_gets_an_admin_role" {
  command = plan

  variables {
    workspace_admin_automation_role_arns = [
      "arn:aws:iam::111122223333:role/adp-dev-superplane-control-plane",
    ]
  }

  assert {
    condition     = length(aws_iam_role.workspace_admin) == 1
    error_message = "a workspace naming an automation role but no human operator created NO admin role. The automated caller then has nothing to assume, and the symptom is a null workspace_admin_role_arn rather than a trust failure. Role creation must be gated on local.workspace_admin_enabled (either list), not on the human list alone."
  }

  assert {
    condition     = length(aws_iam_role_policy.workspace_admin) == 1
    error_message = "the admin role was created without its inline EKS policy, so the automation role can be assumed but cannot describe the cluster it was assumed to reach. Both resources must share the same count expression."
  }

  assert {
    condition     = output.workspace_admin_trust.role_created == true
    error_message = "workspace_admin_trust reports role_created = false while the role is in the plan."
  }

  # Compared by length and element rather than `== [...]`: the output is an object, so
  # Terraform types this attribute as list(string) while a bracket literal in an assertion is
  # a tuple, and `==` across the two is always false ("LHS and RHS values are of different
  # types") — an assertion that can never pass is not a check.
  assert {
    condition     = length(output.workspace_admin_trust.automation_roles_no_mfa) == 1 && output.workspace_admin_trust.automation_roles_no_mfa[0] == "arn:aws:iam::111122223333:role/adp-dev-superplane-control-plane"
    error_message = "workspace_admin_trust does not report the automation role that was named, so a caller cannot confirm from state which list its principal landed in."
  }

  assert {
    condition     = length(output.workspace_admin_trust.human_principals_mfa_required) == 0
    error_message = "workspace_admin_trust reports human principals for a workspace that named none."
  }
}

run "a_human_only_workspace_still_gets_an_admin_role" {
  command = plan

  variables {
    workspace_admin_principal_arns = [
      "arn:aws:iam::111122223333:user/PlatformOperator",
    ]
  }

  assert {
    condition     = length(aws_iam_role.workspace_admin) == 1
    error_message = "the pre-existing human-operator path stopped creating the admin role. The F3 repair must add the automation path without removing this one."
  }

  assert {
    condition     = length(output.workspace_admin_trust.automation_roles_no_mfa) == 0
    error_message = "workspace_admin_trust reports automation roles for a workspace that named none — so an operator reading state would believe an un-MFA'd path exists when it does not."
  }

  assert {
    condition     = length(output.workspace_admin_trust.human_principals_mfa_required) == 1 && output.workspace_admin_trust.human_principals_mfa_required[0] == "arn:aws:iam::111122223333:user/PlatformOperator"
    error_message = "workspace_admin_trust does not report the human principal that was named."
  }
}

run "both_classes_may_be_named_together" {
  command = plan

  variables {
    workspace_admin_principal_arns = [
      "arn:aws:iam::111122223333:user/PlatformOperator",
      "arn:aws:iam::111122223333:user/on-call-engineer",
    ]
    workspace_admin_automation_role_arns = [
      "arn:aws:iam::111122223333:role/adp-dev-superplane-control-plane",
      "arn:aws:iam::111122223333:role/adp-dev-superplane-ci",
    ]
  }

  assert {
    condition     = length(aws_iam_role.workspace_admin) == 1
    error_message = "naming both a human operator and an automation role produced no admin role."
  }

  # One role, one policy — not one per principal. Each principal assumes the SAME role and
  # gets the same scoped permissions; only the trust condition differs between the classes.
  assert {
    condition     = length(aws_iam_role_policy.workspace_admin) == 1
    error_message = "expected exactly one inline policy on the single admin role regardless of how many principals were named."
  }

  assert {
    condition     = length(output.workspace_admin_trust.human_principals_mfa_required) == 2 && length(output.workspace_admin_trust.automation_roles_no_mfa) == 2
    error_message = "workspace_admin_trust did not report both principal classes when both were named."
  }
}

# ---------------------------------------------------------------------------
# The default: no operator named at all. The role must NOT exist — its presence would imply
# an operator does, and a role nobody can assume is still an account-level artifact an
# auditor has to account for.
# ---------------------------------------------------------------------------
run "a_workspace_naming_no_operator_creates_no_admin_role" {
  command = plan

  assert {
    condition     = length(aws_iam_role.workspace_admin) == 0
    error_message = "an admin role was created for a workspace that named neither a human operator nor an automation role."
  }

  assert {
    condition     = output.workspace_admin_role_arn == null
    error_message = "workspace_admin_role_arn is not null when no admin role exists. It must be distinguishable from a valid ARN so a consumer cannot treat \"not configured\" as one."
  }

  assert {
    condition     = output.workspace_admin_trust.role_created == false
    error_message = "workspace_admin_trust reports role_created = true when no role is in the plan."
  }
}

# ---------------------------------------------------------------------------
# FOREIGN AND OVER-BROAD PRINCIPALS ARE REFUSED.
#
# These are what stands in for the MFA condition on the automation statement. That statement
# deliberately carries no condition — a role session cannot present MFA — so the ONLY thing
# keeping it narrow is that its principal list is an exact allowlist of role ARNs. Each run
# below changes exactly one field from the baseline.
# ---------------------------------------------------------------------------
run "an_account_root_automation_principal_is_refused" {
  command = plan

  variables {
    # The shape that looks ordinary and is nearly the worst case: `:root` in a trust policy
    # means ANY principal in that account. On the un-conditioned automation statement that
    # would make the admin role assumable without MFA by every role in the workspace account,
    # including the tenant's own.
    workspace_admin_automation_role_arns = ["arn:aws:iam::111122223333:root"]
  }

  expect_failures = [var.workspace_admin_automation_role_arns]
}

run "a_wildcard_automation_principal_is_refused" {
  command = plan

  variables {
    # IAM does not expand a wildcard in a trust policy principal, so this grants nothing
    # while reading as a grant to every ADP role — the failure mode where the operator
    # believes access is configured and it silently is not.
    workspace_admin_automation_role_arns = ["arn:aws:iam::111122223333:role/adp-*"]
  }

  expect_failures = [var.workspace_admin_automation_role_arns]
}

run "an_iam_user_is_refused_from_the_automation_list" {
  command = plan

  variables {
    # An IAM user is a human-shaped long-lived credential. Admitting one to the list that
    # carries no MFA condition is exactly how the MFA requirement gets bypassed while still
    # appearing in the policy — so users belong in workspace_admin_principal_arns.
    workspace_admin_automation_role_arns = ["arn:aws:iam::111122223333:user/on-call-engineer"]
  }

  expect_failures = [var.workspace_admin_automation_role_arns]
}

run "a_non_arn_automation_principal_is_refused" {
  command = plan

  variables {
    # A bare role NAME rather than an ARN. IAM would not resolve it, and the apply fails with
    # a policy-validation error rather than at plan time where it is cheap to see.
    workspace_admin_automation_role_arns = ["adp-dev-superplane-control-plane"]
  }

  expect_failures = [var.workspace_admin_automation_role_arns]
}

run "a_duplicated_automation_principal_is_refused" {
  command = plan

  variables {
    workspace_admin_automation_role_arns = [
      "arn:aws:iam::111122223333:role/adp-dev-superplane-control-plane",
      "arn:aws:iam::111122223333:role/adp-dev-superplane-control-plane",
    ]
  }

  expect_failures = [var.workspace_admin_automation_role_arns]
}

run "an_account_root_human_principal_is_still_refused" {
  command = plan

  variables {
    # The pre-existing rule on the human list, re-asserted here so the F3 repair cannot be
    # shown correct while having loosened the side it did not change.
    workspace_admin_principal_arns = ["arn:aws:iam::111122223333:root"]
  }

  expect_failures = [var.workspace_admin_principal_arns]
}

run "human_roles_need_an_explicit_federated_authentication_model" {
  command = plan
  variables { workspace_admin_principal_arns = ["arn:aws:iam::111122223333:role/HumanSSO"] }
  expect_failures = [var.workspace_admin_principal_arns]
}

run "overlapping_human_and_automation_trust_is_refused" {
  command = plan
  variables {
    workspace_admin_principal_arns       = ["arn:aws:iam::111122223333:role/HumanSSO"]
    workspace_admin_automation_role_arns = ["arn:aws:iam::111122223333:role/HumanSSO"]
  }
  expect_failures = [var.workspace_admin_principal_arns]
}
