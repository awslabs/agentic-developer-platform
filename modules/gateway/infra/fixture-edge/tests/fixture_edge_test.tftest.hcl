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

  # These two also have server-assigned ids, and the ownership receipt now
  # enumerates them (it previously under-counted, omitting the policy that IS the
  # wrong-role refusal). Without stand-ins the receipt is UNKNOWN at plan time,
  # and an unknown poisons any expression built from it — `try()` recovers from
  # errors, not from unknowns — so the "no output contains the secret" assertion
  # below becomes unevaluable rather than false. That failure mode is worth
  # naming: it means a weaker version of that assertion (one that skipped the
  # receipt) would have passed while leaving the largest output unchecked.
  mock_resource "aws_api_gateway_rest_api_policy" {
    defaults = {
      id = "fx1234abcd"
    }
  }
  mock_resource "aws_api_gateway_deployment" {
    defaults = {
      id = "dep1234abcd"
    }
  }

  # The stage's id is server-assigned too, and it is NOT the stage name: the
  # provider sets it to "ags-<rest-api-id>-<stage-name>"
  # (aws/internal/service/apigateway/stage.go — d.SetId(fmt.Sprintf("ags-%s-%s",
  # apiID, stageName))). The mock reproduces that shape because the receipt used to
  # record the bare `stage_name` instead, which is an identifier no plan ever
  # carries: the destroy guard compares the plan's change.before.id against these
  # ids, so the stage's real deletion line never matched, the guard concluded NOT
  # OWNED, and legitimate teardown was BLOCKED. Root reproduced that with a
  # real-shaped plan. Without a stand-in of the real shape, an assertion here could
  # not tell the two apart.
  mock_resource "aws_api_gateway_stage" {
    defaults = {
      id = "ags-fx1234abcd-dev"
    }
  }

  # The remaining two ids the receipt now records. For these the provider's id IS the
  # resource's name, so the stand-ins are the names main.tf composes under the
  # `variables` block below (name_prefix = "bedrockgw-dev-w2fx-<nonce>").
  #
  # They need stand-ins for a reason worth stating: `id` is a COMPUTED attribute, so
  # it is unknown until apply even when it will equal an attribute that is known now.
  # An unknown inside the `ownership` output makes every expression built from that
  # output unevaluable — `try()` recovers from errors, not from unknowns — so without
  # these the secret-hygiene assertion below stops being checked rather than failing,
  # and 18 later runs cascade to `skip`. Silently losing the largest output's
  # no-secret check is a worse outcome than the mismatch it was added to catch.
  mock_resource "aws_ssm_parameter" {
    defaults = {
      id = "/adp/dev/gateway/fixture/a1b2c3d4e5f60718/apigw-provenance-secret"
    }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = {
      id = "/aws/api-gateway/bedrockgw-dev-w2fx-a1b2c3d4e5f60718-fixture-edge"
      # The stage's access_log_settings.destination_arn is this arn, and the provider
      # VALIDATES it as an ARN during plan. Declaring defaults for this resource makes
      # its generated values deterministic, so the arn has to be a real-shaped one or
      # the stage fails to plan for a reason unrelated to anything under test.
      arn = "arn:aws:logs:us-east-1:879318057152:log-group:/aws/api-gateway/bedrockgw-dev-w2fx-a1b2c3d4e5f60718-fixture-edge"
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

  # REACHABILITY IS NOW READ FROM THE RULES, so the mocks have to supply rules.
  #
  # The previous revision needed no mock here at all, which is itself the point:
  # it compared the ALB's discovered groups against a LIST THE OPERATOR TYPED, so
  # the test could not distinguish "the link may egress here" from "the operator
  # believes it may". These stand-ins describe the dev pair that was verified
  # read-only (sg-013f2ce2bcaf1642c egresses tcp/80 to the shared backend group;
  # that group admits tcp/80 from the link), and every negative run below re-points
  # exactly one of them.
  #
  # `ids` must be mocked as well as the rules: the for_each over
  # data.aws_vpc_security_group_rules.reachability[0].ids drives which rule reads
  # exist at all. Two ids are used, one per direction, so a run can break one
  # direction while leaving the other intact — which is what proves the two
  # preconditions are independent rather than one check stated twice.
  mock_data "aws_vpc_security_group_rules" {
    defaults = {
      ids = ["sgr-link-egress", "sgr-alb-ingress"]
    }
  }

  # A per-instance default cannot be expressed here (mock_data applies to every
  # instance), so the two directions are set by file-level override_data below.
  mock_data "aws_vpc_security_group_rule" {
    defaults = {
      ip_protocol = "tcp"
      from_port   = 80
      to_port     = 80
    }
  }

  # The fixture ALB's own network interfaces. These are what the fixture pod sees
  # as the source of ALB traffic, and therefore what #3968's fixture NetworkPolicy
  # has to admit (blocker 6).
  #
  # Two interfaces, because a real ALB has one per subnet. A single-interface mock
  # would let a bug that publishes only the first address pass.
  mock_data "aws_network_interfaces" {
    defaults = {
      ids = ["eni-fixture-b", "eni-fixture-a"]
    }
  }

  # Deliberately in the WRONG order relative to the sorted output below
  # (eni-fixture-b holds the higher address). for_each iterates a set, whose order
  # is not the author's, so the sort in the output is what makes the value stable —
  # and an unstable value would produce a spurious diff in #3968's rendered policy
  # every plan, which is how someone stops regenerating it.
  mock_data "aws_network_interface" {
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

# The happy-path rules, one per direction. File-level so every run inherits a
# REACHABLE pair; a run that overrides one of these is asserting a specific,
# single-fact break rather than an absence of setup.
#
# Note both rules reference the OTHER side's group by id. That is deliberate and is
# what the fix turns on: a CIDR rule spanning the ALB's subnets is not accepted as
# proof of reachability to THIS load balancer, so a mock that used cidr_ipv4 here
# would (correctly) fail the gate.
override_data {
  target = data.aws_vpc_security_group_rule.reachability["sgr-link-egress"]
  values = {
    is_egress                    = true
    security_group_id            = "sg-013f2ce2bcaf1642c"
    referenced_security_group_id = "sg-0b0f5533ab8440db8"
    ip_protocol                  = "tcp"
    from_port                    = 80
    to_port                      = 80
  }
}

override_data {
  target = data.aws_vpc_security_group_rule.reachability["sgr-alb-ingress"]
  values = {
    is_egress                    = false
    security_group_id            = "sg-0b0f5533ab8440db8"
    referenced_security_group_id = "sg-013f2ce2bcaf1642c"
    ip_protocol                  = "tcp"
    from_port                    = 80
    to_port                      = 80
  }
}

# The ALB's two interfaces, one per subnet, with real-shaped private addresses from
# the dev private subnets (10.0.10.0/24 and 10.0.11.0/24 — read read-only). Set
# per-instance because mock_data applies to every instance alike.
override_data {
  target = data.aws_network_interface.fixture_alb["eni-fixture-a"]
  values = {
    private_ip = "10.0.10.41"
    subnet_id  = "subnet-0860c744097c41a03"
    vpc_id     = "vpc-0d6115bead9301d25"
  }
}

override_data {
  target = data.aws_network_interface.fixture_alb["eni-fixture-b"]
  values = {
    private_ip = "10.0.11.52"
    subnet_id  = "subnet-03ae2ea2ebdf611bb"
    vpc_id     = "vpc-0d6115bead9301d25"
  }
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

  # The ownership output is an INVENTORY RECEIPT, not a delete authority. The
  # previous revision enumerated three resources with name/tag-derived `verify`
  # commands and a `delete = true` flag, which read as "look this up by name and
  # delete it". That is not replacement-safe: a name or tag cannot distinguish the
  # object this run created from a later same-named one. These assertions pin the
  # corrected shape.
  assert {
    # Every EVALUATED resource must appear. The old count of 3 silently omitted the
    # resource policy (the wrong-role refusal), the deployment and the stage, so an
    # inventory built from it could not show the refusal was removed with the API.
    condition     = length(output.ownership.resources) == 6
    error_message = "Ownership must enumerate all six Terraform-owned resources (api, policy, deployment, stage, ssm parameter, log group). The previous revision listed 3 and omitted the resource policy, so the inventory could not account for the wrong-role refusal."
  }

  assert {
    condition = alltrue([
      for r in output.ownership.resources : r.verify != "" && r.kind != ""
    ])
    error_message = "Every ownership row must name its kind and how to probe for its ABSENCE after teardown."
  }

  assert {
    # Every row must carry its TERRAFORM RESOURCE TYPE, because the destroy guard
    # matches plan lines on (type, id) pairs. It cannot match on ids alone: the REST
    # API and its resource policy genuinely SHARE one id (the policy is an attribute
    # of the API), so a set of ids is satisfied by a plan that deletes only the API
    # and an omitted policy — the wrong-role Deny — could not be detected.
    condition = alltrue([
      for r in output.ownership.resources : r.type != "" && startswith(r.type, "aws_")
    ])
    error_message = "Every ownership row must record its Terraform resource type. The destroy guard matches on (type, id) because the REST API and its policy share an id, so an id-only receipt cannot show both were included."
  }

  assert {
    # The receipt's identifiers must be the ones TERRAFORM WILL PRESENT in a plan.
    # This is the assertion that fails on root's executed finding: the stage was
    # recorded as its `stage_name` ("dev"), while the provider's id — and therefore
    # every plan line — is "ags-<rest-api-id>-<stage-name>". The mismatch made the
    # destroy guard refuse a LEGITIMATE teardown, pushing the operator toward
    # deleting by hand, which is the exact outcome the guard exists to prevent.
    condition = alltrue([
      for r in output.ownership.resources :
      r.id == {
        aws_api_gateway_rest_api        = aws_api_gateway_rest_api.fixture[0].id
        aws_api_gateway_rest_api_policy = aws_api_gateway_rest_api_policy.fixture[0].id
        aws_api_gateway_deployment      = aws_api_gateway_deployment.fixture[0].id
        aws_api_gateway_stage           = aws_api_gateway_stage.fixture[0].id
        aws_ssm_parameter               = aws_ssm_parameter.fixture_provenance_secret[0].id
        aws_cloudwatch_log_group        = aws_cloudwatch_log_group.fixture[0].id
      }[r.type]
    ])
    error_message = "Each ownership row's id must be the resource's own provider-assigned .id, since that is what a destroy plan carries. Recording a stage's stage_name instead of its 'ags-<api>-<stage>' id made the guard refuse a legitimate teardown."
  }

  assert {
    # Stated separately and literally, so this cannot silently pass again by both
    # sides drifting together: the stage row must be the ags-prefixed composite, and
    # must NOT be the bare stage name.
    condition = alltrue([
      for r in output.ownership.resources : (
        startswith(r.id, "ags-${aws_api_gateway_rest_api.fixture[0].id}-") &&
        r.id != aws_api_gateway_stage.fixture[0].stage_name
      ) if r.type == "aws_api_gateway_stage"
    ])
    error_message = "The stage row must record the provider's composite id 'ags-<rest-api-id>-<stage-name>', not the bare stage_name. The bare name matches no plan line, so the destroy guard reported the stage as NOT OWNED and blocked teardown."
  }

  assert {
    # All six must be individually accounted for as (type, id) pairs even though only
    # five DISTINCT ids exist across them. Counting ids would give 5 and look like a
    # missing resource; counting pairs is what the guard actually does.
    condition = (
      length(distinct([for r in output.ownership.resources : "${r.type}/${r.id}"])) == 6 &&
      length(distinct([for r in output.ownership.resources : r.id])) == 5
    )
    error_message = "The six rows must be six distinct (type, id) pairs over five distinct ids — the REST API and its policy share an id. If the distinct id count is 6, the shared-id case is no longer being represented and the completeness check is not being tested."
  }

  assert {
    # The receipt must state the real mechanism, so it cannot be misread as
    # authorising a name-prefix sweep.
    condition = (
      strcontains(output.ownership.teardown.mechanism, "terraform destroy") &&
      strcontains(output.ownership.teardown.state_key, var.run_nonce) &&
      strcontains(output.ownership.teardown.state_key, var.expected_account_id)
    )
    error_message = "The teardown receipt must name terraform-destroy-against-per-run-state as the mechanism, with a state key bound to both the account and the nonce."
  }

  assert {
    # Ordering is load-bearing: main.tf READS the fixture ALB, and Terraform
    # re-reads data sources during destroy, so deleting the ALB first makes the
    # destroy unplannable (reproduced on Terraform 1.15.3).
    condition     = strcontains(output.ownership.teardown.must_run_before, "Ingress")
    error_message = "The receipt must record that this edge is destroyed BEFORE the fixture Ingress/ALB, because destroying it re-reads that ALB."
  }

  assert {
    condition     = strcontains(output.ownership.teardown.not_supported, "name")
    error_message = "The receipt must explicitly rule out name/tag-prefix deletion, which cannot distinguish this run's object from a later same-named replacement."
  }

  assert {
    # The two Kubernetes objects are uid-gated in #3968's EXISTING k8s bucket, so
    # no new cleanup type was needed and none of its files were edited.
    condition = length(output.ownership.ledger_owned_k8s) == 2 && alltrue([
      for r in output.ownership.ledger_owned_k8s :
      strcontains(r.recorded, "--uid") && strcontains(r.delete_by, "uid-gated")
    ])
    error_message = "The Ingress and Secret must be recorded as uid-gated ledger objects — a uid is what makes their deletion replacement-safe."
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

# ---------------------------------------------------------------------------
# REACHABILITY — ASSERTED FROM THE LIVE RULES, IN BOTH DIRECTIONS
# ---------------------------------------------------------------------------
# Root's blocker: "read VPC Link and ALL attached SGs and prove real ALB
# ingress/egress; caller lists / setintersection are not proof."
#
# The runs below are the negatives that the previous `setintersection` check could
# not express. Each changes ONE live fact and expects a refusal. They matter
# individually because each corresponds to a fixture that passed every other gate
# and then TIMED OUT — a failure that reads as "the protected worker failed"
# rather than "the fixture was never wired".
#
# What the old check could not catch, and each of these now does:
#   * a group the operator listed but whose rule was since revoked
#   * egress present, ingress absent (the direction never examined at all)
#   * a rule scoped to another port, or to udp
#   * a CIDR rule mistaken for identity-based reachability
#   * a rule belonging to some unrelated group returned by the same read
#
# THE POSITIVE CONTROL COMES FIRST. Without it every run here could pass by the
# gate refusing for an unrelated reason, which is the failure mode of a suite made
# only of negatives.
run "reachability_is_established_from_the_rules_not_from_an_input" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  assert {
    condition     = local.vpc_link_can_reach_fixture_alb
    error_message = "The happy-path rule pair must establish reachability, or every negative run below could be passing for an unrelated reason."
  }

  assert {
    # Both halves must be carried by a REAL, NAMED rule. Asserted separately from
    # the conjunction above so a regression that satisfies one side twice (for
    # example by dropping the is_egress test) is visible here.
    condition = (
      length(local.vpc_link_egress_rules_to_fixture_alb) == 1 &&
      length(local.fixture_alb_ingress_rules_from_vpc_link) == 1
    )
    error_message = "Each direction must be satisfied by exactly one of the two mocked rules. If one list holds both, the egress/ingress distinction is no longer being applied."
  }

  assert {
    # The rules examined must be the ones on the groups the LIVE RESOURCES carry —
    # the link's own security_group_ids and the ALB's own security_groups. An
    # operator-supplied list is what root rejected.
    condition = (
      local.vpc_link_security_group_ids == toset(data.aws_apigatewayv2_vpc_link.reused[0].security_group_ids) &&
      local.fixture_alb_security_group_ids == toset(data.aws_lb.fixture[0].security_groups)
    )
    error_message = "Both sides of the reachability check must be discovered from the live VPC Link and the live ALB."
  }

  assert {
    # EVERY attached group must be in scope for the rule read, not a subset.
    # Asserted against the data source's OWN filter values, so a future edit that
    # narrows the read to (say) only the link's groups fails here rather than
    # quietly stopping the ALB side from being examined.
    condition = alltrue([
      for id in setunion(local.vpc_link_security_group_ids, local.fixture_alb_security_group_ids) :
      contains(one([for f in data.aws_vpc_security_group_rules.reachability[0].filter : f.values if f.name == "group-id"]), id)
    ])
    error_message = "The rule read must filter on every group attached to either side, so a group added to the ALB or the link later is still examined."
  }

  assert {
    # The gate's receipt must record the rules it passed on, so a reviewer of an
    # existing fixture re-reads observed facts instead of re-deriving intent.
    condition = (
      length(terraform_data.run_binding_gate[0].input.reachability.egress_rule_ids) == 1 &&
      length(terraform_data.run_binding_gate[0].input.reachability.ingress_rule_ids) == 1 &&
      terraform_data.run_binding_gate[0].input.reachability.port == var.fixture_alb_listener_port
    )
    error_message = "The run-binding record must name the rule ids that carried each direction and the port they were checked on."
  }
}

# The case the old check DID cover, restated against rules: a brand-new group the
# link has no egress rule for. It still has to refuse.
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
      # A brand-new group. The mocked rules reference sg-0b0f5533ab8440db8, so
      # NEITHER direction resolves — exactly what a controller-created group does.
      security_groups = ["sg-0999999999999999a"]
      tags            = { AdpFixtureRun = "a1b2c3d4e5f60718" }
    }
  }

  expect_failures = [terraform_data.run_binding_gate]
}

# THE DIRECTION THE PREVIOUS REVISION NEVER LOOKED AT.
#
# This is the most important run in the file, because it is the one a fixture ALB
# built exactly as RUNBOOK.md documents can still fail. Reusing a permitted group
# supplies the LINK's egress rule; the inbound rule lives on the ALB's group and
# can be missing or scoped elsewhere. Under `setintersection` this state was
# INDISTINGUISHABLE from a working fixture: the group was in the list, the check
# passed, and every request timed out.
run "refuses_egress_without_a_matching_ingress_rule" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  # Egress from the link is left intact. Only the ALB-side inbound rule is
  # re-pointed, at a group that is not the link's.
  override_data {
    target = data.aws_vpc_security_group_rule.reachability["sgr-alb-ingress"]
    values = {
      is_egress                    = false
      security_group_id            = "sg-0b0f5533ab8440db8"
      referenced_security_group_id = "sg-0aaaaaaaaaaaaaaaa"
      ip_protocol                  = "tcp"
      from_port                    = 80
      to_port                      = 80
    }
  }

  expect_failures = [terraform_data.run_binding_gate]
}

# The mirror case: the ALB admits the link, but the link has no egress rule to the
# ALB's group. Both halves are required, so both must be individually capable of
# refusing — otherwise the conjunction is satisfied by one check written twice.
run "refuses_ingress_without_a_matching_egress_rule" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  override_data {
    target = data.aws_vpc_security_group_rule.reachability["sgr-link-egress"]
    values = {
      is_egress                    = true
      security_group_id            = "sg-013f2ce2bcaf1642c"
      referenced_security_group_id = "sg-0aaaaaaaaaaaaaaaa"
      ip_protocol                  = "tcp"
      from_port                    = 80
      to_port                      = 80
    }
  }

  expect_failures = [terraform_data.run_binding_gate]
}

# PORT. Set membership was true for a group reachable only on 443. The listener
# port is 80 (the only port the link's live egress rule permits), so a rule that
# admits 443 only must not satisfy the gate.
run "refuses_a_rule_that_does_not_admit_the_fixture_port" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  override_data {
    target = data.aws_vpc_security_group_rule.reachability["sgr-link-egress"]
    values = {
      is_egress                    = true
      security_group_id            = "sg-013f2ce2bcaf1642c"
      referenced_security_group_id = "sg-0b0f5533ab8440db8"
      ip_protocol                  = "tcp"
      from_port                    = 443
      to_port                      = 443
    }
  }

  expect_failures = [terraform_data.run_binding_gate]
}

# PROTOCOL. Same groups, same ports, wrong protocol. An ALB listener is TCP, so a
# udp rule is not reachability — and neither `setintersection` nor a port-range
# test alone would notice.
run "refuses_a_rule_for_the_wrong_protocol" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  override_data {
    target = data.aws_vpc_security_group_rule.reachability["sgr-alb-ingress"]
    values = {
      is_egress                    = false
      security_group_id            = "sg-0b0f5533ab8440db8"
      referenced_security_group_id = "sg-013f2ce2bcaf1642c"
      ip_protocol                  = "udp"
      from_port                    = 80
      to_port                      = 80
    }
  }

  expect_failures = [terraform_data.run_binding_gate]
}

# A CIDR IS A LOCATION, NOT AN IDENTITY.
#
# A rule permitting the ALB's subnet CIDRs would let traffic through today, so it
# is tempting to accept. It is refused deliberately: it establishes nothing about
# THIS load balancer, stays true after the ALB is replaced by another in the same
# subnets, and would let the gate pass for a fixture pointed at someone else's ALB.
run "refuses_a_cidr_rule_as_proof_of_reachability" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  override_data {
    target = data.aws_vpc_security_group_rule.reachability["sgr-alb-ingress"]
    values = {
      is_egress                    = false
      security_group_id            = "sg-0b0f5533ab8440db8"
      referenced_security_group_id = null
      cidr_ipv4                    = "10.0.0.0/16"
      ip_protocol                  = "tcp"
      from_port                    = 80
      to_port                      = 80
    }
  }

  expect_failures = [terraform_data.run_binding_gate]
}

# The group-id filter returns rules for EVERY group on either side, and shared
# groups carry plenty of rules that have nothing to do with this path. A rule on
# an unrelated group must not be counted, or the gate would pass on the strength
# of ordinary gateway traffic.
run "ignores_a_rule_that_belongs_to_an_unrelated_security_group" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  override_data {
    target = data.aws_vpc_security_group_rule.reachability["sgr-link-egress"]
    values = {
      is_egress = true
      # Neither the link's group nor the ALB's: some third group the same
      # describe call returned.
      security_group_id            = "sg-0d76484377ffc964d"
      referenced_security_group_id = "sg-0b0f5533ab8440db8"
      ip_protocol                  = "tcp"
      from_port                    = 80
      to_port                      = 80
    }
  }

  expect_failures = [terraform_data.run_binding_gate]
}

# An all-traffic rule ("-1") is the broadest permission AWS has, and the API
# reports its ports as -1. A naive from_port/to_port range test would therefore
# REJECT the most permissive rule in existence — a false refusal that would send an
# operator looking for a networking gap that is not there.
run "accepts_an_all_traffic_rule_despite_its_negative_ports" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  override_data {
    target = data.aws_vpc_security_group_rule.reachability["sgr-link-egress"]
    values = {
      is_egress                    = true
      security_group_id            = "sg-013f2ce2bcaf1642c"
      referenced_security_group_id = "sg-0b0f5533ab8440db8"
      ip_protocol                  = "-1"
      from_port                    = -1
      to_port                      = -1
    }
  }

  assert {
    condition     = local.vpc_link_can_reach_fixture_alb
    error_message = "An ip_protocol=-1 rule permits all traffic; refusing it would be a false negative caused by its -1 port fields."
  }
}

# A WIDE TCP RANGE that contains the port is real reachability. Asserted so the
# port check is a RANGE test and not an equality test on from_port — the live
# shared groups do carry range rules.
run "accepts_a_port_range_that_contains_the_fixture_port" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  override_data {
    target = data.aws_vpc_security_group_rule.reachability["sgr-alb-ingress"]
    values = {
      is_egress                    = false
      security_group_id            = "sg-0b0f5533ab8440db8"
      referenced_security_group_id = "sg-013f2ce2bcaf1642c"
      ip_protocol                  = "tcp"
      from_port                    = 1
      to_port                      = 1024
    }
  }

  assert {
    condition     = local.vpc_link_can_reach_fixture_alb
    error_message = "A tcp range spanning the fixture port is reachability; the check must test containment, not equality."
  }
}

# NO RULES AT ALL must refuse rather than pass vacuously. This is the shape of
# failure that matters most for a gate built on a list comprehension: an empty
# input makes every `for ... if` produce an empty list, and a check written as
# "no DISALLOWED rule was found" would have been satisfied by it. The check is
# written as "a PERMITTING rule was found", so it refuses.
run "refuses_when_no_security_group_rules_are_readable_at_all" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  override_data {
    target = data.aws_vpc_security_group_rules.reachability[0]
    values = {
      ids = []
    }
  }

  expect_failures = [terraform_data.run_binding_gate]
}

# ---------------------------------------------------------------------------
# THE NETWORKPOLICY SEAM WITH #3968 (blocker 6)
# ---------------------------------------------------------------------------
# Root: #3968's fixture gateway policy admits ingress on 8080 only from pods in
# adp-gateway / adp-agents via namespaceSelector; ALB connections are NOT
# namespace-selected pod sources. A narrow ALB source observation had to be agreed
# with #3968, with no global relaxation of the ordinary policy.
#
# The agreed artifact is output.fixture_alb_network_policy_source. What makes it
# worth testing is that every plausible WRONG version of it is silently harmless-
# looking:
#
#   * a subnet CIDR admits the ORDINARY gateway's ALB (they share a subnet), which
#     would let production's edge into the fixture while reading as "narrow"
#   * 0.0.0.0/0 admits the whole VPC
#   * an empty list renders an ipBlock that matches nothing — a denial that reads as
#     a configured policy, and reports as "the protected worker failed"
#   * the listener port (80) instead of the container port (8080) renders a rule
#     that blocks the flow under test
#
# None of those fails to apply. Each is asserted against below.
run "publishes_the_narrow_alb_traffic_source_3968s_policy_must_admit" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  assert {
    # Every interface, not just the first. The mock supplies two because a real ALB
    # has one per subnet, and a fixture policy missing one address fails
    # intermittently — whichever AZ the connection lands in.
    condition     = length(output.fixture_alb_network_policy_source.source_cidrs) == 2
    error_message = "Every one of the ALB's interfaces must be published. A policy missing one address denies whichever AZ that interface serves, which presents as an intermittent bootstrap failure."
  }

  assert {
    # /32 AND NOTHING WIDER. This is the assertion that encodes root's "no global
    # relaxation": a /24 here would admit the ordinary gateway's ALB, which sits in
    # the same subnet (verified read-only in dev).
    condition = alltrue([
      for c in output.fixture_alb_network_policy_source.source_cidrs :
      endswith(c, "/32")
    ])
    error_message = "Sources must be /32 host addresses. A subnet CIDR would admit the ORDINARY gateway's ALB — it shares subnet-03ae2ea2ebdf611bb with the fixture's — so a wider mask silently grants production's edge access to the fixture."
  }

  assert {
    # The addresses must be the ones READ from the ALB's interfaces — not a
    # variable, and not derived from a subnet. Asserted both ways round so the
    # published set can neither omit an interface nor invent an address.
    condition = (
      alltrue([
        for eni in data.aws_network_interface.fixture_alb :
        contains(output.fixture_alb_network_policy_source.source_cidrs, "${eni.private_ip}/32")
      ]) &&
      alltrue([
        for c in output.fixture_alb_network_policy_source.source_cidrs :
        contains([for eni in data.aws_network_interface.fixture_alb : "${eni.private_ip}/32"], c)
      ])
    )
    error_message = "The published CIDRs must be exactly the addresses read from the fixture ALB's own network interfaces — no interface omitted, no address invented."
  }

  assert {
    # STABLE ORDER. for_each iterates a set, so without the sort the list order is
    # not the author's. An unstable output produces a spurious diff in #3968's
    # rendered policy on every plan, and a diff that always appears is a diff
    # nobody reads — which is how a real change to the source set gets missed.
    # Compared as a joined string: the output is a list(string) while the for
    # expression yields a tuple, and `==` across those reports a TYPE mismatch
    # rather than an ordering difference — so the assertion would fail for a reason
    # unrelated to order.
    condition = join(",", output.fixture_alb_network_policy_source.source_cidrs) == join(",", sort([
      for eni in data.aws_network_interface.fixture_alb : "${eni.private_ip}/32"
    ]))
    error_message = "The source list must be sorted. An unstable order re-diffs #3968's policy on every plan, and a permanent diff hides a real one."
  }

  assert {
    # THE PORT THE POLICY RULE NAMES. 8080 is the container port; 80 is the ALB
    # listener. They are different numbers and naming the listener produces a policy
    # that blocks exactly the traffic under test.
    condition = (
      output.fixture_alb_network_policy_source.container_port == 8080 &&
      output.fixture_alb_network_policy_source.alb_listener_port == var.fixture_alb_listener_port &&
      output.fixture_alb_network_policy_source.container_port != output.fixture_alb_network_policy_source.alb_listener_port
    )
    error_message = "The policy rule's port must be the CONTAINER port (8080), published separately from the ALB listener port. #3968's policy already names 8080; a rule naming 80 would block the flow under test."
  }

  assert {
    # The value must say which policy it belongs on. Without this, the obvious
    # misreading is to add the rule to the ordinary gateway's policy, whose
    # selectors #5836 requires preserved.
    condition = (
      strcontains(output.fixture_alb_network_policy_source.apply_to, "FIXTURE") &&
      strcontains(output.fixture_alb_network_policy_source.apply_to, "NOT the ordinary")
    )
    error_message = "The output must name the FIXTURE policy explicitly and rule out the ordinary gateway's, which #5836 preserves."
  }

  assert {
    # Run-bound, so a value captured from a previous run is visibly not this one's.
    # A pasted stale address is a denial, and a denial here looks like a failed
    # bootstrap rather than a stale artifact.
    condition = (
      output.fixture_alb_network_policy_source.run_nonce == var.run_nonce &&
      output.fixture_alb_network_policy_source.alb_arn == data.aws_lb.fixture[0].arn
    )
    error_message = "The source observation must be bound to this run and this ALB."
  }

  assert {
    # THE INTERFACES MUST BE FOUND BY THIS LOAD BALANCER'S IDENTITY.
    #
    # Asserted against the data source's own filter, and it is not redundant with
    # the address assertions above: `mock_data` does not evaluate filters, so a
    # mutation that changed this read to `subnet-id` (which would return EVERY
    # interface in the ALB's subnets — the ordinary gateway's ALB, the nodes, the
    # pods) returned the same mocked ids and no other assertion here noticed. The
    # published set would then admit half the VPC while still being made of /32s.
    #
    # `ELB <arn_suffix>` is the description the ELB service assigns (verified
    # read-only in dev: filtering on the ordinary ALB's suffix returned exactly its
    # two interfaces). arn_suffix comes from the DISCOVERED load balancer, so the
    # filter cannot be pointed elsewhere by a variable.
    # Joined to a string before comparing: the filter's `values` is a list(string)
    # and the literal is a tuple, and `==` across those reports a TYPE mismatch
    # instead of a difference in content. Joining also pins that the filter carries
    # EXACTLY this one value — `contains` would pass for a filter that also listed
    # something wider.
    condition = join(",", one([
      for f in data.aws_network_interfaces.fixture_alb[0].filter : f.values
      if f.name == "description"
    ])) == "ELB ${data.aws_lb.fixture[0].arn_suffix}"
    error_message = "The interfaces must be read by filtering on this load balancer's own 'ELB <arn_suffix>' description. A subnet-id filter would return every interface in the ALB's subnets — including the ORDINARY gateway's ALB and the cluster's pods — and the published /32s would then admit them."
  }

  assert {
    # A reviewer must be able to re-derive it without Terraform, and the command
    # must name THIS ALB — a generic describe-network-interfaces would return every
    # ALB's addresses in the account.
    condition = (
      strcontains(output.fixture_alb_network_policy_source.verify, "describe-network-interfaces") &&
      strcontains(output.fixture_alb_network_policy_source.verify, data.aws_lb.fixture[0].arn_suffix)
    )
    error_message = "The output must carry a read-only command that re-derives THESE addresses from THIS load balancer."
  }
}

# EMPTY MUST REFUSE, NOT PUBLISH.
#
# This is the most important negative in the seam, because the empty case is the one
# that looks handled. An ipBlock rule built from an empty list matches nothing, so
# the fixture pod denies the edge exactly as if the rule were absent — while the
# rendered policy contains an ingress rule and reads as configured. A reviewer
# comparing the policy against this output would see two empty things agreeing.
run "refuses_when_the_albs_traffic_source_cannot_be_observed" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  override_data {
    target = data.aws_network_interfaces.fixture_alb[0]
    values = {
      ids = []
    }
  }

  expect_failures = [terraform_data.run_binding_gate]
}

# The interfaces are matched by a SERVICE-ASSIGNED DESCRIPTION ("ELB <arn_suffix>"),
# which is reliable in practice but is not a documented identity contract. If it ever
# matches something else, publishing that address would have the fixture pod admit
# traffic from an address this edge does not own — a widening, arrived at by
# accident. The VPC check is the cheap guard.
run "refuses_an_observed_interface_outside_the_expected_vpc" {
  command = plan

  variables {
    fixture_edge_enabled = true
  }

  override_data {
    target = data.aws_network_interface.fixture_alb["eni-fixture-b"]
    values = {
      private_ip = "10.9.9.9"
      subnet_id  = "subnet-0999999999999999a"
      vpc_id     = "vpc-08ba938f9cd8c684c"
    }
  }

  expect_failures = [terraform_data.run_binding_gate]
}

# NOTE — "this component cannot change a shared security group" is NOT asserted
# here. Whether a resource is DECLARED is a property of this root's .tf source, and
# a mocked plan is the wrong instrument for it: the assertion would have to read
# the file anyway. It is asserted in
# tests/test_fixture_lifecycle.py::test_no_terraform_file_declares_a_security_group_resource,
# together with the plan-review allowlist that would have to admit such a resource
# for it to reach AWS. Root's constraint is "never change/delete shared SGs
# (sg-0623ec399f4a20b87)", and both sides of the reachability path above are
# shared groups, so the guarantee has to be structural.

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

# REMOVED: refuses_an_empty_vpc_link_egress_allowlist.
#
# It asserted a validation on vpc_link_egress_target_security_group_ids, an input
# that no longer exists — reachability is read from the rules. The property it
# protected (an empty set of permissions must REFUSE, not pass vacuously) is now
# covered by refuses_when_no_security_group_rules_are_readable_at_all above, which
# tests it where it now lives: an empty rule read.

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

run "extra_ipv4_listener_ingress" {
  command = plan
  variables { fixture_edge_enabled = true }
  override_data {
    target = data.aws_vpc_security_group_rules.reachability[0]
    values = { ids = ["sgr-link-egress", "sgr-alb-ingress", "sgr-extra"] }
  }
  override_data {
    target = data.aws_vpc_security_group_rule.reachability["sgr-extra"]
    values = {
      is_egress                    = false
      security_group_id            = "sg-0b0f5533ab8440db8"
      referenced_security_group_id = null
      ip_protocol                  = "tcp"
      from_port                    = 80
      to_port                      = 80
      cidr_ipv4                    = "0.0.0.0/0"
    }
  }
  expect_failures = [terraform_data.run_binding_gate]
}

run "extra_ipv6_listener_ingress" {
  command = plan
  variables { fixture_edge_enabled = true }
  override_data {
    target = data.aws_vpc_security_group_rules.reachability[0]
    values = { ids = ["sgr-link-egress", "sgr-alb-ingress", "sgr-extra"] }
  }
  override_data {
    target = data.aws_vpc_security_group_rule.reachability["sgr-extra"]
    values = {
      is_egress                    = false
      security_group_id            = "sg-0b0f5533ab8440db8"
      referenced_security_group_id = null
      ip_protocol                  = "tcp"
      from_port                    = 80
      to_port                      = 80
      cidr_ipv6                    = "::/0"
    }
  }
  expect_failures = [terraform_data.run_binding_gate]
}

run "extra_all_protocol_ingress" {
  command = plan
  variables { fixture_edge_enabled = true }
  override_data {
    target = data.aws_vpc_security_group_rules.reachability[0]
    values = { ids = ["sgr-link-egress", "sgr-alb-ingress", "sgr-extra"] }
  }
  override_data {
    target = data.aws_vpc_security_group_rule.reachability["sgr-extra"]
    values = {
      is_egress                    = false
      security_group_id            = "sg-0b0f5533ab8440db8"
      referenced_security_group_id = null
      ip_protocol                  = "-1"
      from_port                    = -1
      to_port                      = -1
      cidr_ipv4                    = "0.0.0.0/0"
    }
  }
  expect_failures = [terraform_data.run_binding_gate]
}

run "extra_foreign_group_ingress" {
  command = plan
  variables { fixture_edge_enabled = true }
  override_data {
    target = data.aws_vpc_security_group_rules.reachability[0]
    values = { ids = ["sgr-link-egress", "sgr-alb-ingress", "sgr-extra"] }
  }
  override_data {
    target = data.aws_vpc_security_group_rule.reachability["sgr-extra"]
    values = {
      is_egress                    = false
      security_group_id            = "sg-0b0f5533ab8440db8"
      referenced_security_group_id = "sg-foreign"
      ip_protocol                  = "tcp"
      from_port                    = 80
      to_port                      = 80

    }
  }
  expect_failures = [terraform_data.run_binding_gate]
}

run "second_attached_permissive_group" {
  command = plan
  variables { fixture_edge_enabled = true }
  override_data {
    target = data.aws_lb.fixture[0]
    values = {
      arn             = "arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/w2-fixture-alb/aaaa1111bbbb2222"
      dns_name        = "internal-w2-fixture-alb-123456.us-east-1.elb.amazonaws.com"
      internal        = true
      vpc_id          = "vpc-0d6115bead9301d25"
      security_groups = ["sg-0b0f5533ab8440db8", "sg-extra"]
      tags            = { AdpFixtureRun = "a1b2c3d4e5f60718" }
    }
  }
  override_data {
    target = data.aws_vpc_security_group_rules.reachability[0]
    values = { ids = ["sgr-link-egress", "sgr-alb-ingress", "sgr-extra"] }
  }
  override_data {
    target = data.aws_vpc_security_group_rule.reachability["sgr-extra"]
    values = {
      is_egress                    = false
      security_group_id            = "sg-extra"
      referenced_security_group_id = null
      ip_protocol                  = "tcp"
      from_port                    = 80
      to_port                      = 80
      cidr_ipv4                    = "0.0.0.0/0"
    }
  }
  expect_failures = [terraform_data.run_binding_gate]
}

run "backend_egress_preserved" {
  command = plan
  variables { fixture_edge_enabled = true }
  override_data {
    target = data.aws_vpc_security_group_rules.reachability[0]
    values = { ids = ["sgr-link-egress", "sgr-alb-ingress", "sgr-extra"] }
  }
  override_data {
    target = data.aws_vpc_security_group_rule.reachability["sgr-extra"]
    values = {
      is_egress                    = true
      security_group_id            = "sg-0b0f5533ab8440db8"
      referenced_security_group_id = null
      ip_protocol                  = "tcp"
      from_port                    = 80
      to_port                      = 80
      cidr_ipv4                    = "10.0.0.0/16"
    }
  }
  assert {
    condition     = local.vpc_link_can_reach_fixture_alb
    error_message = "Unrelated backend traffic must preserve listener reachability."
  }
}

run "non_listener_ingress_preserved" {
  command = plan
  variables { fixture_edge_enabled = true }
  override_data {
    target = data.aws_vpc_security_group_rules.reachability[0]
    values = { ids = ["sgr-link-egress", "sgr-alb-ingress", "sgr-extra"] }
  }
  override_data {
    target = data.aws_vpc_security_group_rule.reachability["sgr-extra"]
    values = {
      is_egress                    = false
      security_group_id            = "sg-0b0f5533ab8440db8"
      referenced_security_group_id = null
      ip_protocol                  = "tcp"
      from_port                    = 443
      to_port                      = 443
      cidr_ipv4                    = "10.0.0.0/16"
    }
  }
  assert {
    condition     = local.vpc_link_can_reach_fixture_alb
    error_message = "Unrelated backend traffic must preserve listener reachability."
  }
}
