# Tests for the Wave 2 fixture trusted edge — Issue #5836
#
# These are MOCKED tests: `command = plan` with a mocked AWS provider, so nothing
# is created, no credentials are used and no state is written. They therefore
# prove PROPERTIES OF THE CONFIGURATION, not live behaviour. Live acceptance (a
# protected worker completing a real bootstrap, and the spoofed/unsigned/
# wrong-role refusals) is only established when root executes the runbook — it is
# deliberately NOT claimed from this file.
#
# What is pinned here is the set of facts that fail SILENTLY if they regress: the
# apply succeeds, the fixture looks healthy, and either the trusted headers become
# forgeable or the "isolated" fixture is quietly forwarding to production.

# Mocking the providers keeps these runnable with no AWS account. The caller
# identity is mocked to the authorized account so the run-binding checks pass;
# a separate run below proves a MISMATCH is refused.
mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "879318057152"
    }
  }
  mock_data "aws_region" {
    defaults = {
      region = "us-east-1"
    }
  }

  # The REST API id and execution_arn are server-assigned, so the worker-endpoint
  # and resource-policy assertions would be unevaluable at plan time without a
  # stand-in. These mocks supply the SHAPE only; the assertions below check how
  # the id is composed into the endpoint, not what the id is.
  mock_resource "aws_api_gateway_rest_api" {
    defaults = {
      id            = "fx1234abcd"
      execution_arn = "arn:aws:execute-api:us-east-1:879318057152:fx1234abcd"
    }
  }

  override_during = plan
}

# The generated secret is unknown until apply, so the assertions that inspect the
# INJECTED provenance value would be unevaluable under `command = plan`. Mocking a
# deterministic 64-char stand-in makes those assertions runnable without an apply.
# It is a stand-in for SHAPE and PLACEMENT only — never a real secret, and the
# real value's randomness comes from random_password, not from this file.
mock_provider "random" {
  mock_resource "random_password" {
    defaults = {
      result = "MOCKPROVENANCE00000000000000000000000000000000000000000000000000"
    }
  }
  override_during = plan
}

variables {
  run_nonce           = "a1b2c3d4e5f60718"
  expected_account_id = "879318057152"
  aws_region          = "us-east-1"
  environment         = "dev"

  fixture_alb_arn = "arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/w2-fixture-alb/aaaa1111bbbb2222"
  fixture_alb_dns = "internal-w2-fixture-alb-123456.us-east-1.elb.amazonaws.com"

  ordinary_internal_plane_alb_arn = "arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/k8s-adpgatew-bedrockg-d2e32d8c72/cccc3333dddd4444"

  vpc_link_id = "qmovr6"

  allowed_caller_role_arns = ["arn:aws:iam::879318057152:role/adp-dev-agent-authority-worker"]
}

# ---------------------------------------------------------------------------
# DEFAULT-OFF
# ---------------------------------------------------------------------------
# The single most important property for merge safety. If this regresses, merging
# the PR would create a trusted edge (and an ALB-adjacent cost) in whatever
# account the next ordinary apply runs against.
run "disabled_by_default_creates_nothing" {
  command = plan

  # fixture_edge_enabled intentionally not set — asserting the DEFAULT.

  assert {
    condition     = var.fixture_edge_enabled == false
    error_message = "fixture_edge_enabled must default to false: merging must not create a trusted edge."
  }

  assert {
    condition     = length(aws_api_gateway_rest_api.fixture) == 0
    error_message = "Disabled must create no REST API."
  }

  assert {
    condition     = length(random_password.fixture_edge_provenance) == 0
    error_message = "Disabled must not even generate a provenance secret."
  }

  assert {
    condition     = length(aws_api_gateway_stage.fixture) == 0 && length(aws_api_gateway_deployment.fixture) == 0
    error_message = "Disabled must create no stage and no deployment."
  }

  assert {
    condition     = length(aws_ssm_parameter.fixture_provenance_secret) == 0
    error_message = "Disabled must create no SSM parameter."
  }

  assert {
    condition     = length(aws_api_gateway_rest_api_policy.fixture) == 0
    error_message = "Disabled must create no resource policy."
  }

  assert {
    condition     = output.worker_control_endpoint == "" && output.rest_api_id == ""
    error_message = "Disabled outputs must be empty rather than a half-formed endpoint."
  }
}

# ---------------------------------------------------------------------------
# AUTH MAPPINGS — the trusted-header contract
# ---------------------------------------------------------------------------
run "internal_route_requires_aws_iam_and_injects_verified_identity" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  # AWS_IAM is what makes an unsigned call impossible to serve: API Gateway
  # rejects it before the integration runs, so it can never acquire provenance.
  assert {
    condition     = jsondecode(local.fixture_api_body).paths["/internal/{proxy+}"]["x-amazon-apigateway-any-method"]["x-amazon-apigateway-auth"].type == "AWS_IAM"
    error_message = "The fixture internal route must require AWS_IAM. Without it, unsigned callers would be served AND handed a genuine provenance header."
  }

  assert {
    condition     = length(jsondecode(local.fixture_api_body).paths["/internal/{proxy+}"]["x-amazon-apigateway-any-method"].security) == 1
    error_message = "The internal route must declare exactly the sigv4 security requirement."
  }

  # The identity must come from the signature API Gateway verified, never from
  # the client. context.identity.userArn is the only trustworthy source.
  assert {
    condition     = jsondecode(local.fixture_api_body).paths["/internal/{proxy+}"]["x-amazon-apigateway-any-method"]["x-amazon-apigateway-integration"].requestParameters["integration.request.header.X-Caller-Identity"] == "context.identity.userArn"
    error_message = "X-Caller-Identity on the internal route must be mapped from context.identity.userArn (the verified SigV4 principal)."
  }

  # Provenance must be a static literal ('single-quoted'), i.e. injected by the
  # edge, and must NOT be sourced from any method.request.* value — which would
  # mean the caller could supply it.
  assert {
    condition = can(regex(
      "^'[A-Za-z0-9]{64}'$",
      jsondecode(local.fixture_api_body).paths["/internal/{proxy+}"]["x-amazon-apigateway-any-method"]["x-amazon-apigateway-integration"].requestParameters["integration.request.header.X-Adp-Edge-Provenance"]
    ))
    error_message = "X-Adp-Edge-Provenance must be an edge-injected static literal of the generated 64-char secret, never derived from the request."
  }

  assert {
    condition = !can(regex(
      "method\\.request",
      jsondecode(local.fixture_api_body).paths["/internal/{proxy+}"]["x-amazon-apigateway-any-method"]["x-amazon-apigateway-integration"].requestParameters["integration.request.header.X-Adp-Edge-Provenance"]
    ))
    error_message = "Provenance must not be sourced from the request — that would let a caller supply its own proof."
  }
}

# The human-session plane must BLANK both headers. This is a real requirement,
# not defence in depth: src/auth/dependencies.py skips the provenance branch only
# when no X-Caller-Identity is asserted, so a forwarded client header changes
# which authentication branch executes.
run "human_route_blanks_caller_and_provenance" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  assert {
    condition     = jsondecode(local.fixture_api_body).paths["/{proxy+}"]["x-amazon-apigateway-any-method"]["x-amazon-apigateway-auth"].type == "NONE"
    error_message = "The human route must be auth NONE — the fixture gateway authenticates it on its JWT."
  }

  assert {
    condition     = jsondecode(local.fixture_api_body).paths["/{proxy+}"]["x-amazon-apigateway-any-method"]["x-amazon-apigateway-integration"].requestParameters["integration.request.header.X-Caller-Identity"] == "''"
    error_message = "The auth-NONE route must BLANK X-Caller-Identity. Unmapped forwards the client's value, which the pod treats as proof of identity."
  }

  assert {
    condition     = jsondecode(local.fixture_api_body).paths["/{proxy+}"]["x-amazon-apigateway-any-method"]["x-amazon-apigateway-integration"].requestParameters["integration.request.header.X-Adp-Edge-Provenance"] == "''"
    error_message = "The auth-NONE route must BLANK X-Adp-Edge-Provenance."
  }

  # No security scheme on the human route: asserting the two planes really are
  # distinct rather than both signed or both open.
  assert {
    condition     = !can(jsondecode(local.fixture_api_body).paths["/{proxy+}"]["x-amazon-apigateway-any-method"].security)
    error_message = "The human route must not declare a sigv4 requirement."
  }
}

# The #5653-style invariant, restated as a test so the intent is reviewable even
# though the postcondition also enforces it at plan time: EVERY route with an
# integration maps BOTH headers. A future route that forgets reopens a forgeable
# path.
run "every_route_maps_both_trusted_headers" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  assert {
    condition = alltrue([
      for path_key, path_item in jsondecode(local.fixture_api_body).paths :
      alltrue([
        for required in [
          "integration.request.header.X-Caller-Identity",
          "integration.request.header.X-Adp-Edge-Provenance",
        ] : contains(keys(path_item["x-amazon-apigateway-any-method"]["x-amazon-apigateway-integration"].requestParameters), required)
      ])
      if can(path_item["x-amazon-apigateway-any-method"]["x-amazon-apigateway-integration"])
    ])
    error_message = "Every fixture route with an integration must map BOTH X-Caller-Identity and X-Adp-Edge-Provenance."
  }

  # Exactly two planes today. A third route appearing without a deliberate test
  # update should draw a reviewer's attention.
  assert {
    condition     = length(keys(jsondecode(local.fixture_api_body).paths)) == 2
    error_message = "Expected exactly the internal and human routes; a new route needs its own auth-mapping assertions."
  }
}

# ---------------------------------------------------------------------------
# TRANSPORT PATH — traced from the worker, not assumed
# ---------------------------------------------------------------------------
run "forwards_internal_prefix_and_publishes_https_worker_endpoint" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  # The pod registers routes under /internal/v1/agent, so dropping the prefix
  # would 404 every bootstrap while the edge itself looked healthy.
  assert {
    condition     = jsondecode(local.fixture_api_body).paths["/internal/{proxy+}"]["x-amazon-apigateway-any-method"]["x-amazon-apigateway-integration"].uri == "http://${var.fixture_alb_dns}/internal/{proxy}"
    error_message = "The internal route must forward with the /internal prefix preserved (the pod registers its routes under it)."
  }

  # run_identity.py rejects any endpoint that is not https with a bare host, and
  # appends /bootstrap itself — so the published base must be https and must
  # already include /internal/v1/agent.
  assert {
    condition     = startswith(output.worker_control_endpoint, "https://")
    error_message = "worker_control_endpoint must be https — run_identity.py rejects every other scheme."
  }

  assert {
    condition     = endswith(output.worker_control_endpoint, "/internal/v1/agent")
    error_message = "worker_control_endpoint must end at /internal/v1/agent; the worker appends /bootstrap itself."
  }

  # No userinfo, query or fragment — each is independently rejected by the client.
  assert {
    condition     = !can(regex("[@?#]", output.worker_control_endpoint))
    error_message = "worker_control_endpoint must have no userinfo, query or fragment — run_identity.py rejects all three."
  }

  # Reuse of the shared VPC link, and the ALB bound per-integration.
  assert {
    condition     = jsondecode(local.fixture_api_body).paths["/internal/{proxy+}"]["x-amazon-apigateway-any-method"]["x-amazon-apigateway-integration"].connectionId == var.vpc_link_id
    error_message = "The fixture must REUSE the existing VPC link rather than duplicate platform plumbing."
  }

  assert {
    condition     = jsondecode(local.fixture_api_body).paths["/internal/{proxy+}"]["x-amazon-apigateway-any-method"]["x-amazon-apigateway-integration"].integrationTarget == var.fixture_alb_arn
    error_message = "integrationTarget must be the FIXTURE ALB load balancer ARN."
  }
}

# ---------------------------------------------------------------------------
# ISOLATION — the fixture must not be production wearing a fixture label
# ---------------------------------------------------------------------------
run "never_forwards_to_the_ordinary_internal_plane" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  assert {
    condition     = var.fixture_alb_arn != var.ordinary_internal_plane_alb_arn
    error_message = "The fixture ALB must differ from the ordinary internal-plane ALB."
  }

  # Both planes must forward to the FIXTURE ALB and nothing else. Checked across
  # every route rather than just the internal one, because a fixture whose human
  # plane pointed at production would leak real sessions into the evaluation.
  assert {
    condition = alltrue([
      for path_key, path_item in jsondecode(local.fixture_api_body).paths :
      path_item["x-amazon-apigateway-any-method"]["x-amazon-apigateway-integration"].integrationTarget == var.fixture_alb_arn
    ])
    error_message = "Every fixture route must forward to the fixture ALB, never the ordinary internal-plane ALB."
  }

  assert {
    condition = alltrue([
      for path_key, path_item in jsondecode(local.fixture_api_body).paths :
      strcontains(path_item["x-amazon-apigateway-any-method"]["x-amazon-apigateway-integration"].uri, var.fixture_alb_dns)
    ])
    error_message = "Every fixture route's forward URI must address the fixture ALB DNS."
  }

  # A fresh secret per run, NOT the ordinary gateway's. If the fixture shared
  # production's secret, headers minted by either edge would be accepted by the
  # other and the isolation would be nominal only.
  assert {
    condition     = length(random_password.fixture_edge_provenance) == 1
    error_message = "Enabled must generate its own provenance secret."
  }

  assert {
    condition     = random_password.fixture_edge_provenance[0].keepers["run_nonce"] == var.run_nonce
    error_message = "The provenance secret must be keyed to the run nonce so a new run cannot inherit a previous run's proof."
  }

  # The fixture's SSM path must be run-scoped and must NOT collide with the
  # ordinary gateway's provenance parameter.
  assert {
    condition     = aws_ssm_parameter.fixture_provenance_secret[0].name == "/adp/dev/gateway/fixture/${var.run_nonce}/apigw-provenance-secret"
    error_message = "The fixture provenance parameter must live under a run-scoped fixture path."
  }

  assert {
    condition     = aws_ssm_parameter.fixture_provenance_secret[0].name != "/adp/dev/gateway/apigw-provenance-secret"
    error_message = "The fixture must never write the ORDINARY gateway's provenance parameter."
  }

  assert {
    condition     = aws_ssm_parameter.fixture_provenance_secret[0].type == "SecureString"
    error_message = "The provenance parameter must be a SecureString."
  }
}

# ---------------------------------------------------------------------------
# SECRET HYGIENE
# ---------------------------------------------------------------------------
run "provenance_secret_is_not_published" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  # Only the NAME is published. The value is fetched by the fixture tooling with
  # --with-decryption, so it never enters an output, state artifact or log.
  assert {
    condition     = output.ssm_provenance_parameter_name == aws_ssm_parameter.fixture_provenance_secret[0].name
    error_message = "The parameter NAME should be published for the handoff."
  }

  assert {
    condition = !strcontains(
      jsonencode({
        endpoint  = output.worker_control_endpoint
        api       = output.rest_api_id
        ssm       = output.ssm_provenance_parameter_name
        ownership = output.ownership
        enabled   = output.fixture_edge_enabled
        nonce     = output.run_nonce
      }),
      random_password.fixture_edge_provenance[0].result
    )
    error_message = "No output may contain the provenance secret value — outputs are written to state and echoed to the console."
  }

  # The access-log format must not capture request headers, or CloudWatch would
  # hold the secret for the retention window.
  assert {
    condition = !strcontains(
      aws_api_gateway_stage.fixture[0].access_log_settings[0].format,
      "Provenance"
    )
    error_message = "The access-log format must not log the provenance header."
  }

  assert {
    condition = !strcontains(
      aws_api_gateway_stage.fixture[0].access_log_settings[0].format,
      "X-Caller-Identity"
    )
    error_message = "The access-log format must not echo request headers; use context.identity.userArn for the verified principal."
  }
}

# ---------------------------------------------------------------------------
# LEAST PRIVILEGE — wrong-role refusal without touching ordinary IAM
# ---------------------------------------------------------------------------
run "resource_policy_allows_only_listed_roles" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  assert {
    condition = length([
      for s in jsondecode(aws_api_gateway_rest_api_policy.fixture[0].policy).Statement :
      s if s.Effect == "Allow"
    ]) == 1
    error_message = "Exactly one Allow statement expected."
  }

  # Compared as sorted sets rather than with `==`: jsondecode yields a tuple and
  # the variable is a list(string), so a direct comparison fails on type even when
  # the ARNs match.
  assert {
    condition = sort([
      for p in jsondecode(aws_api_gateway_rest_api_policy.fixture[0].policy).Statement[0].Principal.AWS : p
    ]) == sort(var.allowed_caller_role_arns)
    error_message = "The Allow must name exactly the permitted role ARNs."
  }

  # The explicit Deny is what produces the wrong-role refusal the evaluation has
  # to demonstrate: with the worker's wildcard-on-API-id grant, any signed
  # principal could otherwise invoke the fixture.
  assert {
    condition = length([
      for s in jsondecode(aws_api_gateway_rest_api_policy.fixture[0].policy).Statement :
      s if s.Effect == "Deny"
    ]) == 1
    error_message = "A Deny for unlisted principals is required — without it a wrong-role caller would be served."
  }

  # The assumed-role session form must be covered: a SigV4 signature from an
  # assumed session presents sts::assumed-role/NAME/SESSION, not the iam::role
  # ARN, so omitting it would deny the legitimate worker too.
  assert {
    condition = contains(
      jsondecode(aws_api_gateway_rest_api_policy.fixture[0].policy).Statement[1].Condition.StringNotLike["aws:PrincipalArn"],
      "arn:aws:sts::879318057152:assumed-role/adp-dev-agent-authority-worker/*"
    )
    error_message = "The Deny exception must cover the assumed-role SESSION form, or the real worker would be refused."
  }

  assert {
    condition = alltrue([
      for arn in jsondecode(aws_api_gateway_rest_api_policy.fixture[0].policy).Statement[1].Condition.StringNotLike["aws:PrincipalArn"] :
      !strcontains(arn, ":root") && arn != "*"
    ])
    error_message = "The Deny exception list must not admit the account root or everyone."
  }

  # The policy must be part of the redeployment trigger: a policy change that is
  # not redeployed leaves the Deny inert while the plan looks correct — an
  # unrestricted trusted edge reporting as restricted.
  #
  # The expected hash is RECOMPUTED here from the body and the policy. A weaker
  # check (e.g. "the trigger is 40 chars") would pass even if the policy had been
  # dropped from the hash, which is the regression that matters.
  assert {
    condition = aws_api_gateway_deployment.fixture[0].triggers["redeployment"] == sha1(jsonencode([
      local.fixture_api_body,
      aws_api_gateway_rest_api_policy.fixture[0].policy,
    ]))
    error_message = "The deployment trigger must be a sha1 over BOTH the API body and the resource policy."
  }
}

# ---------------------------------------------------------------------------
# OWNERSHIP / RUN BINDING
# ---------------------------------------------------------------------------
run "resources_are_bound_to_run_account_and_region" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  assert {
    condition     = strcontains(aws_api_gateway_rest_api.fixture[0].name, var.run_nonce)
    error_message = "The REST API name must carry the run nonce so two runs cannot collide."
  }

  # Ownership tags must survive common_tags: teardown depends on them.
  assert {
    condition     = aws_api_gateway_rest_api.fixture[0].tags["AdpFixtureRun"] == var.run_nonce
    error_message = "Every resource must carry the AdpFixtureRun tag."
  }

  assert {
    condition     = aws_api_gateway_rest_api.fixture[0].tags["AdpFixtureAccount"] == var.expected_account_id && aws_api_gateway_rest_api.fixture[0].tags["AdpFixtureRegion"] == var.aws_region
    error_message = "Resources must be bound to the account and region as well as the nonce."
  }

  assert {
    condition     = aws_api_gateway_rest_api.fixture[0].tags["Disposable"] == "true"
    error_message = "The fixture edge must be marked disposable."
  }

  # The ownership output is what the #3968 ledger consumes, and each row must
  # carry a re-verifiable identity: a name prefix alone is not ownership.
  assert {
    condition     = length(output.ownership.resources) == 3
    error_message = "Ownership must enumerate all three created resources (api, ssm parameter, log group) so bounded cleanup can reach each one."
  }

  assert {
    condition = alltrue([
      for r in output.ownership.resources :
      r.run_tag == var.run_nonce && r.delete == true && r.verify != ""
    ])
    error_message = "Every ownership row must carry the run tag, a delete flag and a re-verification command."
  }

  assert {
    condition     = output.ownership.account_id == var.expected_account_id && output.ownership.region == var.aws_region && output.ownership.run_nonce == var.run_nonce
    error_message = "The ownership record must bind account, region and nonce."
  }
}

# ---------------------------------------------------------------------------
# FOREIGN / MISSING INPUTS ARE REFUSED
# ---------------------------------------------------------------------------
# #5836 requires tests for "failure of missing/foreign inputs". Each of these
# expects the plan to FAIL.

run "refuses_a_foreign_account" {
  command = plan

  variables {
    fixture_edge_enabled = true
    # Mocked caller identity is 879318057152; this claims a different target.
    expected_account_id = "000000000000"
    fixture_alb_arn     = "arn:aws:elasticloadbalancing:us-east-1:000000000000:loadbalancer/app/w2-fixture-alb/aaaa1111bbbb2222"
  }

  expect_failures = [check.run_binding]
}

run "refuses_a_region_mismatch" {
  command = plan

  variables {
    fixture_edge_enabled = true
    aws_region           = "us-west-2"
    fixture_alb_arn      = "arn:aws:elasticloadbalancing:us-west-2:879318057152:loadbalancer/app/w2-fixture-alb/aaaa1111bbbb2222"
    fixture_alb_dns      = "internal-w2-fixture-alb-123456.us-west-2.elb.amazonaws.com"
  }

  expect_failures = [check.run_binding]
}

# The most dangerous misconfiguration: a "fixture" that actually forwards to the
# ordinary internal plane would report isolation while exercising live traffic.
run "refuses_targeting_the_ordinary_internal_plane_alb" {
  command = plan

  variables {
    fixture_edge_enabled = true
    fixture_alb_arn      = "arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/k8s-adpgatew-bedrockg-d2e32d8c72/cccc3333dddd4444"
  }

  expect_failures = [check.run_binding]
}

run "refuses_a_malformed_run_nonce" {
  command = plan

  variables {
    fixture_edge_enabled = true
    run_nonce            = "PLACEHOLDER"
  }

  expect_failures = [var.run_nonce]
}

# A listener ARN silently fails at apply with "not a valid ALB or NLB arn"; the
# ordinary module learned this against the live API, so it is rejected at plan.
run "refuses_a_listener_arn_as_the_fixture_backend" {
  command = plan

  variables {
    fixture_edge_enabled = true
    fixture_alb_arn      = "arn:aws:elasticloadbalancing:us-east-1:879318057152:listener/app/w2-fixture-alb/aaaa1111bbbb2222/eeee5555"
  }

  expect_failures = [var.fixture_alb_arn]
}

run "refuses_an_empty_caller_allowlist" {
  command = plan

  variables {
    fixture_edge_enabled     = true
    allowed_caller_role_arns = []
  }

  expect_failures = [var.allowed_caller_role_arns]
}

run "refuses_a_wildcard_caller" {
  command = plan

  variables {
    fixture_edge_enabled     = true
    allowed_caller_role_arns = ["arn:aws:iam::879318057152:role/*"]
  }

  expect_failures = [var.allowed_caller_role_arns]
}
