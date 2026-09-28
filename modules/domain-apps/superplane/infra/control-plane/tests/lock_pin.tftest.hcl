# The lock is the only source of what runs — Issue #5042 (U3), EPIC #4910.
#
# config.tf reads U2's releases/superplane.lock.yaml and derives the SkyPilot image
# reference it publishes to SSM. This file asserts the deploy side of R2 from within
# Terraform: the reference this module hands to the rollout is digest-addressed, is the
# digest the lock actually pins, and carries no tag.
#
# WHY THIS IS NOT REDUNDANT WITH U2's PYTHON SUITE
#
# `tests/test_manifests_reference_digests.py` (U2) checks manifests, and today there are
# none — every content test in it skips. `tests/test_lock.py` checks the lock file itself.
# Neither can see what THIS module computes: `local.skypilot_image` is a format() over three
# lock fields, and a change to that expression — a tag appended, the digest dropped, the
# wrong key read — would leave both suites green while this module published an unpinned
# reference to the parameter the rollout consumes.
#
# The failure that matters is specific: the rollout lane reads
# /adp/<env>/superplane/skypilot-image and puts that string in a pod spec. If it is not a
# digest, nothing downstream can say what ran, which is the exact defect the lock exists to
# close (upstream ships berkeleyskypilot/skypilot:latest in all three of its SkyPilot
# manifests).
#
# WHY THE DIGEST IS RESTATED HERE AND THAT IS DELIBERATE
#
# The assertion below hardcodes the lock's digest rather than re-reading the lock. A test
# that re-read the lock to build its own expectation would assert only that format() is
# self-consistent — it would pass if the lock's digest were replaced with a tag, because both
# sides would move together. Restating it means the lock cannot be edited to something
# unpinned without a test failing and naming what changed. It is a pin on a pin, and updating
# both in the same change is the intended cost.

mock_provider "aws" {
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
    }
  }
}

run "the_published_skypilot_reference_is_digest_pinned" {
  command = plan

  # The digest U2's lock pins for skypilot-api, restated on purpose — see the header.
  assert {
    condition     = local.skypilot_digest == "sha256:2c9964592ad9f10e6090113982d2051311ecff2e0331b87f6d354ca116730cb4"
    error_message = "the module must publish the digest the lock pins for skypilot-api. If the lock was intentionally re-pinned, update this expected value in the same change — that is what makes a silent re-pin impossible."
  }

  # Shape, independent of the specific value: a 64-hex sha256 and nothing else.
  assert {
    condition     = can(regex("^sha256:[0-9a-f]{64}$", local.skypilot_digest))
    error_message = "the SkyPilot digest must be a bare sha256: digest of 64 hex characters."
  }

  # The full reference the rollout consumes. `@` separates a digest; `:` after the final
  # `/` would be a tag. Both are checked because a reference can contain a digest and
  # still be tag-addressed in practice (`repo:tag@sha256:...` is valid and resolves by
  # digest, but records a tag that may not match).
  assert {
    condition     = strcontains(local.skypilot_image, "@${local.skypilot_digest}")
    error_message = "the published image reference must be digest-addressed with the pinned digest."
  }

  assert {
    condition     = !strcontains(local.skypilot_image, ":latest")
    error_message = "the published image reference must never name :latest — that is upstream's defect (berkeleyskypilot/skypilot:latest in all three of its SkyPilot manifests)."
  }

  assert {
    condition     = !can(regex("[^/]+:[^@/]+@", local.skypilot_image))
    error_message = "the published image reference must carry a digest ONLY, with no tag component. A tag alongside a digest records a mutable name that may later disagree with what actually ran."
  }
}

# ---------------------------------------------------------------------------
# The parameter the rollout actually reads. Asserting `local.skypilot_image` alone would
# leave a gap: the local could be correct while the SSM parameter published a different
# expression. This checks the resource attribute the rollout consumes by name.
# ---------------------------------------------------------------------------
run "the_ssm_parameter_the_rollout_reads_carries_the_pin" {
  command = plan

  assert {
    condition     = aws_ssm_parameter.skypilot_image.value == local.skypilot_image
    error_message = "the SSM parameter must publish exactly the derived pinned reference."
  }

  assert {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", aws_ssm_parameter.skypilot_image.value))
    error_message = "/adp/<env>/superplane/skypilot-image must end in a sha256 digest: the rollout lane puts this string straight into a pod spec, so an unpinned value here is an unpinned deploy."
  }
}

# ---------------------------------------------------------------------------
# THE PENDING IMAGES MUST NOT ACQUIRE A DEPLOYABLE REFERENCE HERE.
#
# The lock records superplane-api, -controller and -platform-monitor as `pending_images`
# with NO digest, blocked by `source_access`. This module therefore must not publish an
# image reference for any of them — there is no digest to publish, and the failure mode is
# specific: a parameter named like an image reference, holding a tag, that the rollout would
# deploy believing it was pinned.
#
# ECR repositories for them are legitimate (a repository is a destination, not a reference),
# so this asserts the absence of a REFERENCE, not the absence of the repositories.
# ---------------------------------------------------------------------------
run "no_image_reference_is_published_for_a_pending_image" {
  # `apply`, not `plan`, for the same reason as platform_isolation.tftest.hcl's wildcard
  # sweep: two of the parameters below carry computed role ARNs, so at plan time their
  # values are unknown, and a sweep over ALL parameters is therefore unknown. Terraform
  # reports "Condition expression could not be evaluated at this time" and — worse than
  # failing — skips every later run in the file.
  #
  # Narrowing the sweep to the parameters whose values come from configuration would remove
  # the error while removing the point: a fabricated image reference is most likely to be
  # introduced by a NEW parameter, and pre-filtering to today's known-static ones would
  # exempt exactly that. Against the mocked provider an apply reaches no AWS API.
  command = apply

  # Every parameter this module publishes, listed explicitly. Terraform has no way to
  # enumerate resources of a type, so a new `aws_ssm_parameter` will not be covered here
  # automatically — `tests/test_platform_isolation.py::test_every_ssm_parameter_is_checked_for_pins`
  # closes that gap by parsing config.tf and failing if a parameter is added without being
  # added to this list.
  assert {
    condition = length([
      for value in [
        aws_ssm_parameter.skypilot_image.value,
        aws_ssm_parameter.control_plane_role_arn.value,
        aws_ssm_parameter.skypilot_role_arn.value,
        aws_ssm_parameter.namespace.value,
        aws_ssm_parameter.skypilot_namespace.value,
        aws_ssm_parameter.aws_region.value,
        aws_ssm_parameter.cors_allowed_origins.value,
        aws_ssm_parameter.database_secret_name.value,
        aws_ssm_parameter.database_schema.value,
        aws_ssm_parameter.jwt_secret_name.value,
        aws_ssm_parameter.workspace_cluster_context.value,
      ] :
      value if can(regex("superplane-(api|controller|platform-monitor)(:|@)", value))
    ]) == 0
    error_message = "no parameter may publish an image reference for one of the three pending Superplane images. The lock pins no digest for them (blocked by source_access), so any such reference would be either a fabricated pin or a floating tag."
  }
}

run "skypilot_tagged_rollback_images_do_not_expire" {
  command = plan

  assert {
    condition = alltrue([
      for rule in jsondecode(aws_ecr_lifecycle_policy.superplane["adp-superplane-skypilot"].policy).rules :
      rule.selection.tagStatus == "untagged"
    ])
    error_message = "SkyPilot tagged release and rollback images must never match an expiration rule."
  }
  assert {
    condition     = length(jsondecode(aws_ecr_lifecycle_policy.superplane["adp-superplane-skypilot"].policy).rules) == 1
    error_message = "Retain the bounded untagged-image cleanup policy."
  }
  assert {
    condition     = length(jsondecode(aws_ecr_lifecycle_policy.superplane["adp-superplane-api"].policy).rules) == 2
    error_message = "The SkyPilot exception must not remove other repositories' retention limits."
  }
}
