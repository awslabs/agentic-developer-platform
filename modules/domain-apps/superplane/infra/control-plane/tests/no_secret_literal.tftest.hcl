# Acceptance criterion 3, second half: no secret literal in an applied artifact.
# Issue #5042 (U3), EPIC #4910.
#
# THE UPSTREAM DEFECTS THIS BLOCKS
#
# The snapshot ships credentials inside artifacts that get APPLIED, which is what makes
# them expensive: once applied they exist in a cluster, in a shell history, in a CI log, and
# rotating them means rotating everywhere they were copied to.
#
#   deploy/db-migrate-job.yaml   DATABASE_URL as a plain `value:` — a postgresql+asyncpg
#                                URI whose user and password are both the literal service
#                                name. The issue names this file as a SHAPE reference only,
#                                and this is why.
#   deploy/integration-test.yaml a kind: Secret with stringData JWT_SECRET_KEY holding a
#                                plaintext phrase, and DATABASE_URL beside it
#   app/config.py                jwt_secret_key defaulting to a committed placeholder
#   infra/skypilot-api/config.env POSTGRES_PASSWORD=CHANGE_ME
#
# HOW THIS MODULE MAKES THAT UNREPRESENTABLE
#
# It accepts secret NAMES only. There is no variable a secret VALUE could be passed
# through, so there is nothing for a plan or state file to leak — the pod resolves the
# secret at runtime through its scoped IRSA role. These runs assert the validations reject
# value-shaped input, and that what the module publishes really is a reference.
#
# WHAT THIS FILE DOES NOT COVER
#
# A literal hardcoded directly into a manifest or a .tf file is a text-level defect that a
# plan cannot see. tests/test_no_secret_literal.py scans the applied artifacts as text for
# that. This file covers the input boundary; that one covers the files.
#
# NOTE ON THE FIXTURES BELOW: every "secret-shaped" value here is a deliberately fake
# structural example chosen to exercise a regex. None is real, and none is copied from the
# snapshot — reproducing the actual literals in order to test that we reject them would
# reintroduce exactly what the criterion forbids.

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
      eks_oidc_provider_arn = "arn:aws:iam::111122223333:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B716D3041E"
      eks_oidc_issuer       = "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B716D3041E"
    }
  }
}

# ---------------------------------------------------------------------------
# The exact upstream shape: a connection URI where a secret name belongs.
# ---------------------------------------------------------------------------
run "a_connection_uri_is_rejected_as_a_database_secret_name" {
  command = plan

  variables {
    # Structurally identical to upstream's db-migrate-job.yaml DATABASE_URL — scheme,
    # embedded user:password, host — with invented credentials.
    database_secret_name = "postgresql+asyncpg://placeholder:placeholder@postgres.superplane.svc.cluster.local:5432/superplane"
  }

  expect_failures = [var.database_secret_name]
}

# The same defect without a URI scheme. `user:pass@host` is still inline credentials, and a
# check that only looked for "://" would pass this.
run "embedded_credentials_without_a_scheme_are_rejected" {
  command = plan

  variables {
    database_secret_name = "placeholder:placeholder@postgres.superplane.svc.cluster.local"
  }

  expect_failures = [var.database_secret_name]
}

# ---------------------------------------------------------------------------
# A pasted signing key where a secret name belongs. The discriminator is shape: a secret
# name is a short path-like identifier; a signing key is long and has no separators.
# ---------------------------------------------------------------------------
run "a_pasted_signing_key_is_rejected_as_a_jwt_secret_name" {
  command = plan

  variables {
    # 88 characters, no "/" — the shape of base64 key material, not of a secret name.
    # Invented for this test.
    jwt_secret_name = "aGVsbG90aGlzaXNub3RhcmVhbGtleWl0aXNqdXN0YXNoYXBlZXhhbXBsZWZvcnRlc3RpbmdwdXJwb3Nlc29ubHk"
  }

  expect_failures = [var.jwt_secret_name]
}

# ---------------------------------------------------------------------------
# The positive half: legitimate vault-path secret NAMES are accepted, and what the module
# publishes is the reference — not the material.
# ---------------------------------------------------------------------------
run "vault_path_secret_names_are_accepted_and_only_references_are_published" {
  command = plan

  variables {
    database_secret_name = "adp/dev/superplane/database"
    jwt_secret_name      = "adp/dev/superplane/jwt-signing-key"
  }

  assert {
    condition     = aws_ssm_parameter.database_secret_name.value == "adp/dev/superplane/database"
    error_message = "the database secret NAME must be published as a reference for the pod to resolve at runtime."
  }

  # The published values must be references, not material. Asserted by shape rather than by
  # trusting the input, so a future change that started interpolating a resolved value
  # would fail here.
  assert {
    condition     = !strcontains(aws_ssm_parameter.database_secret_name.value, "://")
    error_message = "a published database reference must never contain a connection URI."
  }

  assert {
    condition     = !can(regex("[^/]+:[^/@]+@", aws_ssm_parameter.database_secret_name.value))
    error_message = "a published database reference must never contain embedded credentials."
  }

  # The IAM grant is scoped to the two named secrets, so even a correct reference cannot be
  # widened into "read every secret in the account". A wildcard here would make the
  # reference-not-value discipline pointless.
  assert {
    condition = alltrue([
      for arn in data.aws_iam_policy_document.control_plane.statement[0].resources :
      !endswith(arn, ":secret:*") && !endswith(arn, ":*:*")
    ])
    error_message = "secret read permission must be scoped to the named secrets, not to all secrets in the account."
  }
}

# ---------------------------------------------------------------------------
# No secret VALUE can be published even in principle, because no variable carries one.
# This run pins the *absence* of such a channel by asserting that everything the module
# writes to SSM is a reference or non-secret configuration.
# ---------------------------------------------------------------------------
run "nothing_secret_shaped_is_written_to_parameter_store" {
  command = plan

  # Each published parameter is either an identifier (role ARN, namespace, secret name), a
  # digest-pinned image, or the validated CORS allowlist. None is secret material, and the
  # type is String rather than SecureString precisely because none of it is a secret —
  # using SecureString here would misrepresent non-secret config as secret.
  assert {
    condition = alltrue([
      aws_ssm_parameter.database_secret_name.type == "String",
      aws_ssm_parameter.jwt_secret_name.type == "String",
      aws_ssm_parameter.cors_allowed_origins.type == "String",
    ])
    error_message = "these parameters hold references and configuration, not secrets."
  }

  # A JWT reference must not itself look like key material.
  assert {
    condition     = length(aws_ssm_parameter.jwt_secret_name.value) <= 64 || strcontains(aws_ssm_parameter.jwt_secret_name.value, "/")
    error_message = "the published JWT reference must be a secret name, not key material."
  }
}
