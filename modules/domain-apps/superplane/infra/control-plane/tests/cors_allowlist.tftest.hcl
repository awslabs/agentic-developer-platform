# Acceptance criterion 4: `*` with credentials allowed must fail.
# Issue #5042 (U3), EPIC #4910.
#
# THE UPSTREAM DEFECT THIS BLOCKS
#
# src/superplane-api/app/config.py declares `cors_origins: list[str] = ["*"]` as the
# pydantic-settings DEFAULT, and app/main.py consumes it as
#
#     CORSMiddleware(allow_origins=settings.cors_origins, allow_credentials=True,
#                    allow_methods=["*"], allow_headers=["*"])
#
# The snapshot's deploy/config.env (`CORS_ORIGINS=*`) and integration-test.yaml
# (`CORS_ORIGINS: '["*"]'`) carry it into applied artifacts too — the test manifest even
# concedes in a comment that production needs explicit origins, which is how a default like
# this survives review and then ships.
#
# Blast radius per the issue: "any origin can make credentialed calls against the domain
# API."
#
# WHY THE CHECK IS "MEMBER OF THE LIST", NOT "EQUALS THE LIST"
#
# The obvious implementation — reject `cors_allowed_origins == ["*"]` — passes
# `["https://legit.example", "*"]`, which is just as permissive. So the validation uses
# `contains()`, and `wildcard_hidden_among_valid_origins` below is the run that
# distinguishes the two implementations. Without it, a weaker module passes this file.

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
# The criterion, stated directly: upstream's exact default is rejected.
# ---------------------------------------------------------------------------
run "wildcard_with_credentials_is_rejected" {
  command = plan

  variables {
    cors_allowed_origins   = ["*"]
    cors_allow_credentials = true
  }

  # Both the pairing rule and the origin-format rule are declared on
  # `cors_allowed_origins`, so that is the checkable object that reports the error. It is
  # deliberately NOT `cors_allow_credentials`: a pairing rule declared on that variable is
  # never evaluated, because Terraform skips validations whose referenced variable is
  # already invalid. See the comment in variables.tf.
  expect_failures = [var.cors_allowed_origins]
}

# ---------------------------------------------------------------------------
# THE RUN THAT SEPARATES A REAL IMPLEMENTATION FROM A SUPERFICIAL ONE.
#
# A wildcard smuggled into an otherwise legitimate allowlist is exactly as permissive as a
# bare `["*"]`, and is far likelier to survive review. An `== ["*"]` check passes this.
# ---------------------------------------------------------------------------
run "wildcard_hidden_among_valid_origins" {
  command = plan

  variables {
    cors_allowed_origins = [
      "https://superplane.dev.adp.internal",
      "*",
    ]
    cors_allow_credentials = true
  }

  expect_failures = [var.cors_allowed_origins]
}

# Wildcard *patterns* are the other way this leaks: `https://*.example.com` reads like an
# allowlist entry but is a pattern, and whether it is honoured as one depends on the
# middleware. Rejecting it keeps the allowlist literal.
run "wildcard_subdomain_pattern_is_rejected" {
  command = plan

  variables {
    cors_allowed_origins = ["https://*.adp.internal"]
  }

  expect_failures = [var.cors_allowed_origins]
}

# An empty list is not an allowlist. Without this, `[]` would satisfy "contains no
# wildcard" and pass — a module that allows nothing is a different bug, but still a bug,
# and it would surface as an unexplained CORS outage rather than a plan failure.
run "an_empty_allowlist_is_rejected" {
  command = plan

  variables {
    cors_allowed_origins = []
  }

  expect_failures = [var.cors_allowed_origins]
}

# A bare hostname is not an origin. The Origin header is always scheme-qualified, so an
# entry without a scheme silently never matches — an allowlist that looks correct and
# rejects every request.
run "a_bare_hostname_is_rejected" {
  command = plan

  variables {
    cors_allowed_origins = ["superplane.dev.adp.internal"]
  }

  expect_failures = [var.cors_allowed_origins]
}

# ---------------------------------------------------------------------------
# The positive half: an explicit allowlist is accepted, and is what actually reaches the
# pods. A validation that rejected everything would pass every run above.
# ---------------------------------------------------------------------------
run "an_explicit_allowlist_is_accepted_and_published" {
  command = plan

  variables {
    cors_allowed_origins = [
      "https://superplane.dev.adp.internal",
      "https://adp.dev.internal:8443",
    ]
    cors_allow_credentials = true
  }

  # Terraform decides the allowlist and publishes it, so the value the API applies is the
  # one that passed validation — the rollout does not get to substitute its own.
  assert {
    condition     = aws_ssm_parameter.cors_allowed_origins.value == jsonencode(["https://superplane.dev.adp.internal", "https://adp.dev.internal:8443"])
    error_message = "the validated allowlist must be published for the API to consume, so the deployed value is the one that passed validation."
  }

  assert {
    condition     = !strcontains(aws_ssm_parameter.cors_allowed_origins.value, "\"*\"")
    error_message = "the published allowlist must never contain a wildcard."
  }
}

# `allow_credentials = false` is a genuinely different security model: without credentials
# a wildcard origin is the documented, safe way to serve a public read-only API. The
# validation is scoped to the dangerous pairing rather than banning `*` unconditionally,
# and this run pins that scope so a later "simplification" to an unconditional ban is a
# visible behaviour change rather than a silent one.
run "wildcard_without_credentials_is_not_blocked_by_the_credentials_rule" {
  command = plan

  variables {
    cors_allowed_origins   = ["*"]
    cors_allow_credentials = false
  }

  # Still fails — but on the origin-FORMAT rule only, not the credentials pairing rule.
  # That is the distinction being pinned.
  expect_failures = [var.cors_allowed_origins]
}
