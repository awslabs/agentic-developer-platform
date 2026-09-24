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

  # The fixture ALB is now DISCOVERED rather than described by input strings, so
  # the mock has to supply the facts the blocking gate reads. The happy-path
  # defaults describe a correctly-built fixture ALB: internal, in the expected
  # account/region/VPC, tagged for this run, and carrying a security group the
  # reused VPC Link is already permitted to reach.
  #
  # Each `override_data` run below re-points ONE of these facts to prove the gate
  # refuses — which is only meaningful because the default is otherwise valid.
  mock_data "aws_lb" {
    defaults = {
      arn             = "arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/w2-fixture-alb/aaaa1111bbbb2222"
      dns_name        = "internal-w2-fixture-alb-123456.us-east-1.elb.amazonaws.com"
      internal        = true
      vpc_id          = "vpc-0d6115bead9301d25"
      security_groups = ["sg-0b0f5533ab8440db8"]
      tags = {
        AdpFixtureRun = "a1b2c3d4e5f60718"
      }
    }
  }

  mock_data "aws_apigatewayv2_vpc_link" {
    defaults = {
      vpc_link_id        = "qmovr6"
      subnet_ids         = ["subnet-03ae2ea2ebdf611bb", "subnet-0860c744097c41a03"]
      security_group_ids = ["sg-013f2ce2bcaf1642c"]
    }
  }

  mock_data "aws_subnet" {
    defaults = {
      vpc_id = "vpc-0d6115bead9301d25"
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

  expected_vpc_id = "vpc-0d6115bead9301d25"

  # Both the ARN and the DNS name of the ordinary internal plane are required now:
  # a stale ARN alone would let the isolation check pass while the fixture still
  # fronts the live gateway's pods.
  ordinary_internal_plane_alb_arn = "arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/k8s-adpgatew-bedrockg-d2e32d8c72/cccc3333dddd4444"
  ordinary_internal_plane_alb_dns = "internal-k8s-adpgatew-bedrockg-d2e32d8c72-254378198.us-east-1.elb.amazonaws.com"

  vpc_link_id                               = "qmovr6"
  vpc_link_egress_target_security_group_ids = ["sg-0623ec399f4a20b87", "sg-0d76484377ffc964d", "sg-0b0f5533ab8440db8"]

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
    condition     = jsondecode(local.fixture_api_body).paths["/internal/{proxy+}"]["x-amazon-apigateway-any-method"]["x-amazon-apigateway-integration"].uri == "http://${local.fixture_alb_dns_discovered}/internal/{proxy}"
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
      strcontains(path_item["x-amazon-apigateway-any-method"]["x-amazon-apigateway-integration"].uri, local.fixture_alb_dns_discovered)
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

  # -------------------------------------------------------------------------
  # The Deny must be SCOPED TO THE INTERNAL PLANE.
  # -------------------------------------------------------------------------
  # REGRESSION GUARD (this assertion fails on the pre-fix revision). The earlier
  # policy denied every principal without a matching aws:PrincipalArn across
  # `<execution_arn>/*` — the WHOLE API, including the auth-NONE human routes. An
  # unsigned browser request carries no aws:PrincipalArn, so it matched the Deny
  # and was refused AT THE EDGE before the fixture pod could authenticate its JWT.
  # The human plane was therefore unreachable while the policy read as a
  # tightening. A Deny resource of ".../*" is the specific defect.
  assert {
    condition = alltrue([
      for s in jsondecode(aws_api_gateway_rest_api_policy.fixture[0].policy).Statement :
      # flatten() normalizes a single-string Resource and a list of them to one
      # list. A ternary cannot: `tolist(x)` and `[x]` are list-of-string vs
      # tuple, which do not unify.
      alltrue([for r in flatten([s.Resource]) : strcontains(r, "/internal")])
      if s.Effect == "Deny"
    ])
    error_message = "Every Deny must be scoped to /internal resources. An API-wide Deny also blocks the auth-NONE human routes, whose callers have no aws:PrincipalArn — the fixture's human sign-in path would be refused at the edge before the pod ever sees the JWT."
  }

  # POSITIVE CONTROL for the human transport plane: it must be explicitly allowed
  # without a signature. Its safety comes from header blanking (asserted in
  # `human_route_blanks_caller_and_provenance`), not from blocking transport.
  assert {
    condition = length([
      for s in jsondecode(aws_api_gateway_rest_api_policy.fixture[0].policy).Statement :
      s if s.Effect == "Allow" && s.Sid == "AllowHumanSessionTransport"
    ]) == 1
    error_message = "The human-session transport plane must be explicitly allowed: the fixture pod authenticates those requests from their own JWT, so refusing them at the edge breaks the path this fixture is supposed to serve."
  }

  # NEGATIVE for the internal plane: the listed role is allowed there...
  assert {
    condition = sort([
      for p in [
        for s in jsondecode(aws_api_gateway_rest_api_policy.fixture[0].policy).Statement :
        s if s.Sid == "AllowListedFixtureCallersOnInternal"
      ][0].Principal.AWS : p
    ]) == sort(var.allowed_caller_role_arns)
    error_message = "The internal-plane Allow must name exactly the permitted role ARNs."
  }

  # ...and every other principal is denied there. This is the layer that produces
  # the WRONG-ROLE refusal: the worker's existing grant is wildcard-on-API-id, so
  # without this statement any signed principal could invoke the fixture.
  assert {
    condition = length([
      for s in jsondecode(aws_api_gateway_rest_api_policy.fixture[0].policy).Statement :
      s if s.Effect == "Deny" && s.Sid == "DenyNonListedPrincipalsOnInternal"
    ]) == 1
    error_message = "A Deny for unlisted principals on /internal is required — without it a wrong-role caller would be served."
  }

  # aws:PrincipalArn for a request signed by an assumed role is the underlying IAM
  # ROLE ARN. The pre-fix revision also listed invented
  # `arn:aws:sts::...:assumed-role/NAME/*` entries, with a comment claiming a
  # session presents that form; that claim was false and the entries widened the
  # match for no benefit. Pinned so they cannot be reintroduced.
  assert {
    condition = sort([
      for s in jsondecode(aws_api_gateway_rest_api_policy.fixture[0].policy).Statement :
      s if s.Sid == "DenyNonListedPrincipalsOnInternal"
    ][0].Condition.StringNotLike["aws:PrincipalArn"]) == sort(var.allowed_caller_role_arns)
    error_message = "The Deny exception must be exactly the IAM role ARNs. aws:PrincipalArn resolves to the ROLE ARN for assumed-role requests, so sts::assumed-role/NAME/SESSION variants are unnecessary and only widen the match."
  }

  assert {
    condition = alltrue([
      for arn in [
        for s in jsondecode(aws_api_gateway_rest_api_policy.fixture[0].policy).Statement :
        s if s.Sid == "DenyNonListedPrincipalsOnInternal"
      ][0].Condition.StringNotLike["aws:PrincipalArn"] :
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
# FOREIGN / MISSING INPUTS ARE REFUSED — BLOCKING, NOT WARNING
# ---------------------------------------------------------------------------
# #5836 requires tests for "failure of missing/foreign inputs". Each of these
# expects the plan to FAIL.
#
# WHY THESE NOW TARGET terraform_data.run_binding_gate
# -----------------------------------------------------
# The pre-fix revision expected failures from `check.run_binding`. That made the
# suite GREEN while the guard did not actually gate anything: a failing `check`
# assertion emits a WARNING and `terraform plan` STILL EXITS 0 (reproduced on
# Terraform 1.15.3, matching root's reproduction on 1.14.9). `expect_failures` on
# a check block is satisfied by that warning, so the tests certified a refusal
# that would not have happened — an operator wrapper gating on the exit code would
# have accepted a fixture aimed at the wrong account.
#
# The requirements now live in resource preconditions, which DO fail the plan with
# a non-zero exit. These runs therefore assert a real gate. Treat any future
# attempt to move them back into a `check` block as a regression.
#
# Several cases below use `override_data` to change what DISCOVERY returns rather
# than what the operator typed, because the point of the fix is that isolation is
# established from live facts. A string-only negative could not exercise these.

run "refuses_a_foreign_account" {
  command = plan

  variables {
    fixture_edge_enabled = true
    # Mocked caller identity is 879318057152; this claims a different target.
    expected_account_id = "000000000000"
    fixture_alb_arn     = "arn:aws:elasticloadbalancing:us-east-1:000000000000:loadbalancer/app/w2-fixture-alb/aaaa1111bbbb2222"
  }

  expect_failures = [terraform_data.run_binding_gate]
}

run "refuses_a_region_mismatch" {
  command = plan

  variables {
    fixture_edge_enabled = true
    aws_region           = "us-west-2"
    fixture_alb_arn      = "arn:aws:elasticloadbalancing:us-west-2:879318057152:loadbalancer/app/w2-fixture-alb/aaaa1111bbbb2222"
  }

  expect_failures = [terraform_data.run_binding_gate]
}

# The most dangerous misconfiguration: a "fixture" that actually forwards to the
# ordinary internal plane would report isolation while exercising live traffic.
run "refuses_targeting_the_ordinary_internal_plane_alb" {
  command = plan

  variables {
    fixture_edge_enabled = true
    fixture_alb_arn      = "arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/k8s-adpgatew-bedrockg-d2e32d8c72/cccc3333dddd4444"
  }

  expect_failures = [terraform_data.run_binding_gate]
}

# A DIFFERENT ARN can still front the SAME pods if the operator supplied a stale
# ordinary ARN. Discovery returns the ordinary plane's DNS name here while the ARN
# comparison passes, so only the DNS comparison can catch it. The pre-fix revision
# had no DNS comparison at all.
run "refuses_a_fixture_alb_whose_dns_is_the_ordinary_plane" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  override_data {
    target = data.aws_lb.fixture[0]
    values = {
      arn      = "arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/w2-fixture-alb/aaaa1111bbbb2222"
      dns_name = "internal-k8s-adpgatew-bedrockg-d2e32d8c72-254378198.us-east-1.elb.amazonaws.com"
      internal = true
      vpc_id   = "vpc-0d6115bead9301d25"
      # tolist() ordering is irrelevant; membership is what the gate checks.
      security_groups = ["sg-0b0f5533ab8440db8"]
      tags            = { AdpFixtureRun = "a1b2c3d4e5f60718" }
    }
  }

  expect_failures = [terraform_data.run_binding_gate]
}

# An INTERNET-FACING backend behind a trusted-header-injecting edge would let the
# fixture gateway be addressed directly, bypassing the only component allowed to
# assert identity. Scheme is a property of the load balancer, so this can only be
# caught by reading it — the pre-fix revision inferred "internal" from a DNS
# pattern, which is not even reliable (the dev LiteLLM ALB is internal with no
# `internal-` prefix).
run "refuses_a_public_fixture_alb" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  override_data {
    target = data.aws_lb.fixture[0]
    values = {
      arn             = "arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/w2-fixture-alb/aaaa1111bbbb2222"
      dns_name        = "w2-fixture-alb-123456.us-east-1.elb.amazonaws.com"
      internal        = false
      vpc_id          = "vpc-0d6115bead9301d25"
      security_groups = ["sg-0b0f5533ab8440db8"]
      tags            = { AdpFixtureRun = "a1b2c3d4e5f60718" }
    }
  }

  expect_failures = [terraform_data.run_binding_gate]
}

# A fixture ALB in another VPC applies cleanly and then times out on every call,
# which reads as a broken fixture rather than as a misconfiguration.
run "refuses_a_fixture_alb_in_a_foreign_vpc" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  override_data {
    target = data.aws_lb.fixture[0]
    values = {
      arn             = "arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/w2-fixture-alb/aaaa1111bbbb2222"
      dns_name        = "internal-w2-fixture-alb-123456.us-east-1.elb.amazonaws.com"
      internal        = true
      vpc_id          = "vpc-08ba938f9cd8c684c"
      security_groups = ["sg-0b0f5533ab8440db8"]
      tags            = { AdpFixtureRun = "a1b2c3d4e5f60718" }
    }
  }

  expect_failures = [terraform_data.run_binding_gate]
}

# OWNERSHIP. Without this the component would attach a trusted edge to ANY
# internal ALB in the VPC — ordinary infrastructure, or another run's fixture —
# and teardown could not prove what this run was responsible for. A name or ARN
# alone is not ownership.
run "refuses_a_fixture_alb_not_tagged_for_this_run" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  override_data {
    target = data.aws_lb.fixture[0]
    values = {
      arn             = "arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/w2-fixture-alb/aaaa1111bbbb2222"
      dns_name        = "internal-w2-fixture-alb-123456.us-east-1.elb.amazonaws.com"
      internal        = true
      vpc_id          = "vpc-0d6115bead9301d25"
      security_groups = ["sg-0b0f5533ab8440db8"]
      # Tagged for a DIFFERENT run.
      tags = { AdpFixtureRun = "ffffffffffffffff" }
    }
  }

  expect_failures = [terraform_data.run_binding_gate]
}

# REACHABILITY is not implied by sharing a VPC. The reused VPC Link's security
# group permits egress only to SPECIFIC target security groups (verified
# read-only in dev: sg-013f2ce2bcaf1642c allows tcp/80 to three named ALB groups,
# not open egress). A fixture ALB with a fresh controller-created group passes
# every other check and then times out on every request.
run "refuses_a_fixture_alb_the_vpc_link_cannot_reach" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  override_data {
    target = data.aws_lb.fixture[0]
    values = {
      arn      = "arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/w2-fixture-alb/aaaa1111bbbb2222"
      dns_name = "internal-w2-fixture-alb-123456.us-east-1.elb.amazonaws.com"
      internal = true
      vpc_id   = "vpc-0d6115bead9301d25"
      # A brand-new group the link has no egress rule for.
      security_groups = ["sg-0999999999999999a"]
      tags            = { AdpFixtureRun = "a1b2c3d4e5f60718" }
    }
  }

  expect_failures = [terraform_data.run_binding_gate]
}

# The reused VPC Link must itself be in the expected VPC. Its VPC is derived from
# a subnet because the provider's vpc_link data source exports no vpc_id
# (confirmed against the hashicorp/aws v6 schema).
run "refuses_a_vpc_link_in_a_foreign_vpc" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  override_data {
    target = data.aws_subnet.vpc_link[0]
    values = {
      vpc_id = "vpc-08ba938f9cd8c684c"
    }
  }

  expect_failures = [terraform_data.run_binding_gate]
}

# MISSING ORDINARY IDENTITY must REFUSE, not silently skip.
# In the pre-fix revision ordinary_internal_plane_alb_arn defaulted to "", so the
# "not the ordinary ALB" comparison passed trivially exactly when the operator had
# not looked the value up — the check was skipped at the moment it mattered most.
# Not having discovered the ordinary identity is not evidence of isolation.
run "refuses_a_missing_ordinary_alb_identity" {
  command = plan

  variables {
    fixture_edge_enabled            = true
    ordinary_internal_plane_alb_arn = ""
  }

  expect_failures = [var.ordinary_internal_plane_alb_arn]
}

run "refuses_a_missing_ordinary_alb_dns" {
  command = plan

  variables {
    fixture_edge_enabled            = true
    ordinary_internal_plane_alb_dns = ""
  }

  expect_failures = [var.ordinary_internal_plane_alb_dns]
}

run "refuses_an_empty_vpc_link_egress_allowlist" {
  command = plan

  variables {
    fixture_edge_enabled                      = true
    vpc_link_egress_target_security_group_ids = []
  }

  expect_failures = [var.vpc_link_egress_target_security_group_ids]
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
