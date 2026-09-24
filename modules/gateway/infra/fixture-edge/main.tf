# =============================================================================
# Wave 2 fixture trusted edge — Issue #5836
# =============================================================================
# WHY THIS EXISTS
# ---------------
# Epic #3959 Wave 2 acceptance (#3968) needs a PROTECTED agent worker to complete
# a genuine bootstrap against an ISOLATED fixture gateway. That is impossible
# through the ordinary edge, and the reason is structural rather than a matter of
# configuration:
#
#   * Every /internal/v1/agent route is gated by require_agent_transport
#     (modules/gateway/src/agentauth/routes.py:50-57), which 403s without
#     X-Caller-Identity.
#   * That header is believed ONLY when X-Adp-Edge-Provenance constant-time
#     matches the configured secret (src/auth/caller_provenance.py:89-101). A
#     failed provenance check is terminal — never a fallback. So the pair cannot
#     be fabricated, which is also why #5836 forbids trying.
#   * Only API Gateway injects that pair, and every AWS_IAM route forwards to a
#     SINGLE ALB ARN that terminates at Service bedrockgateway -> pods labelled
#     `app: bedrockgateway`. Genuine-provenance traffic therefore cannot reach a
#     fixture.
#
# #3968's FIXTURE-ROUTING-CONSTRAINT.md (at e8ceb349a) traced every candidate
# diversion — host headers, stage variables, canary, a second Ingress on the same
# ALB — found none available on this EKS Auto Mode cluster, and named the owning
# change instead of faking a result. THIS COMPONENT IS THAT NAMED CHANGE.
#
# WHY A SEPARATE COMPONENT AND NOT A ROUTE IN THE ORDINARY MODULE
# ---------------------------------------------------------------
# #5836 requires ordinary flags, routes, selectors and the unknown-owner probe to
# be preserved, and forbids any ordinary platform apply. Adding a fixture route to
# modules/api-gateway would put disposable per-run state inside the production
# REST API: the fixture's lifecycle would be entangled with production's
# sha1(body) redeployment trigger, and every fixture create/destroy would
# redeploy the live stage. A standalone REST API with its OWN isolated state
# backend cannot do that. Ordinary traffic is preserved STRUCTURALLY — this
# directory touches no production resource — rather than by reviewer vigilance.
#
# WHAT IS DELIBERATELY NOT HERE
# -----------------------------
#   * No VPC, subnets, NAT or VPC Link creation. The existing VPC Link is reused
#     (#5836: no duplicated broad platform infrastructure, no new NAT).
#   * No IAM role, policy or permissions-boundary change. None is needed; see
#     the least-privilege note on the resource policy below.
#   * No fixture ALB/Ingress/Deployment. Those are Kubernetes objects owned by
#     the #3968 fixture tooling, which this component must not edit concurrently.
#     This component consumes the fixture ALB as an input and owns only the edge.
# =============================================================================

terraform {
  required_version = ">= 1.14.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  enabled = var.fixture_edge_enabled

  # Every name carries the run nonce, so a second run cannot collide with this
  # one and teardown can tell them apart by name as well as by tag.
  name_prefix = "bedrockgw-${var.environment}-w2fx-${var.run_nonce}"

  # Ownership tags are merged LAST so var.common_tags cannot override the facts
  # teardown relies on. #3968's lib/ownership.py verifies a server-returned
  # identity at teardown and deletes only on a match; these tags are the
  # API-Gateway-side equivalent of its SQS run-nonce tag.
  ownership_tags = {
    AdpFixtureRun     = var.run_nonce
    AdpFixtureIssue   = "5836"
    AdpFixtureEpic    = "3959"
    AdpFixtureAccount = var.expected_account_id
    AdpFixtureRegion  = var.aws_region
    Environment       = var.environment
    ManagedBy         = "terraform"
    Purpose           = "wave2-fixture-trusted-edge"
    Disposable        = "true"
  }
  tags = merge(var.common_tags, local.ownership_tags)
}

# =============================================================================
# Run binding — fail before creating anything in the wrong place
# =============================================================================
# These are checks, not resources. They run at plan time, so a misdirected
# fixture fails in review rather than after creating a trusted edge in an
# account nobody authorized.

check "run_binding" {
  assert {
    condition     = !local.enabled || data.aws_caller_identity.current.account_id == var.expected_account_id
    error_message = "Refusing: the caller's real account does not match expected_account_id (#5836 authorizes 879318057152 only)."
  }

  assert {
    condition     = !local.enabled || data.aws_region.current.region == var.aws_region
    error_message = "Refusing: the provider's resolved region does not match aws_region."
  }

  assert {
    condition     = !local.enabled || var.fixture_alb_arn != var.ordinary_internal_plane_alb_arn
    error_message = <<-EOT
      Refusing: fixture_alb_arn equals the ORDINARY internal-plane ALB.

      This edge would inject genuine trusted headers and forward them to the
      ordinary gateway pods, so the "fixture" evaluation would actually be
      exercising live traffic while reporting isolation. That is precisely the
      dishonest outcome #3968's FIXTURE-ROUTING-CONSTRAINT.md refused to ship.
    EOT
  }

  # The fixture ALB must live in the same account as the one authorized. An ALB
  # ARN embeds its account, so this is checkable without an API call.
  assert {
    condition     = !local.enabled || can(regex(":${var.expected_account_id}:", var.fixture_alb_arn))
    error_message = "Refusing: fixture_alb_arn belongs to a different account than expected_account_id."
  }
}

# =============================================================================
# Fixture provenance secret — FRESH, and never the ordinary one
# =============================================================================
# The pod believes X-Caller-Identity only when X-Adp-Edge-Provenance matches the
# secret THAT POD is configured with. The fixture gateway is configured with this
# value, so:
#
#   * a request that did not pass through this fixture edge cannot produce it, and
#   * this edge's header is worthless against the ORDINARY gateway, whose secret
#     is a different random_password in the production module.
#
# The isolation therefore runs in both directions, which is what makes the
# fixture safe to point a real protected worker at. Generated per run (it is
# bound to the nonce via keepers), never read from the ordinary SSM path.
resource "random_password" "fixture_edge_provenance" {
  count = local.enabled ? 1 : 0

  length  = 64
  special = false

  # Rotating with the nonce guarantees a new run never inherits a previous run's
  # proof — a leaked fixture secret cannot outlive its run.
  keepers = {
    run_nonce = var.run_nonce
  }
}

locals {
  # Mapping-template syntax: 'single quotes' denote a static string literal, and
  # context.identity.userArn is the principal API GATEWAY resolved from a
  # signature it verified itself. The worker never supplies either value.
  fixture_verified_caller_identity = local.enabled ? {
    "integration.request.header.X-Caller-Identity"     = "context.identity.userArn"
    "integration.request.header.X-Adp-Edge-Provenance" = "'${random_password.fixture_edge_provenance[0].result}'"
  } : {}

  # Any non-AWS_IAM route must BLANK both headers rather than leave them
  # unmapped. Unmapped means "forward whatever the client sent", and the pod
  # treats X-Caller-Identity as proof of identity — so an unmapped header on an
  # auth-NONE path hands an unauthenticated caller a privileged TokenContext.
  # Blank is what API Gateway can actually guarantee, and the pod's
  # `headers.get(...).strip()` makes blank equivalent to absent.
  fixture_blank_caller_identity = {
    "integration.request.header.X-Caller-Identity"     = "''"
    "integration.request.header.X-Adp-Edge-Provenance" = "''"
  }

  # -------------------------------------------------------------------------
  # Forwarded path — traced from the worker, not assumed
  # -------------------------------------------------------------------------
  # lib/run_identity.py builds `base + "/bootstrap"` from
  # ADP_AGENT_CONTROL_ENDPOINT and REJECTS any endpoint that is not https with a
  # bare host (no userinfo/query/fragment) — so an in-cluster http Service URL
  # cannot be substituted, and the fixture MUST be fronted by a real HTTPS edge.
  # Routes are registered on the pod WITH the /internal prefix
  # (APIRouter(prefix="/internal/v1/agent")), so the prefix must be preserved on
  # forward or every call 404s. The ordinary module makes the same choice for the
  # same reason.
  fixture_internal_forward_uri = "http://${var.fixture_alb_dns}/internal/{proxy}"

  fixture_api_body = jsonencode({
    openapi = "3.0.1"
    info = {
      title   = "${local.name_prefix}-fixture-edge"
      version = "1.0"
    }
    paths = {
      # -------------------------------------------------------------------
      # The trusted internal plane. AWS_IAM is REQUIRED here: API Gateway
      # verifies the SigV4 signature and only then writes the identity it
      # derived. An unsigned call never reaches the integration, so it can
      # never acquire the provenance header — this is why "unsigned calls are
      # refused" holds by construction rather than by application logic.
      # -------------------------------------------------------------------
      "/internal/{proxy+}" = {
        x-amazon-apigateway-any-method = {
          security                   = [{ sigv4 = [] }]
          "x-amazon-apigateway-auth" = { type = "AWS_IAM" }
          parameters = [
            {
              name     = "proxy"
              in       = "path"
              required = true
              type     = "string"
            }
          ]
          x-amazon-apigateway-integration = {
            type                 = "http_proxy"
            httpMethod           = "ANY"
            uri                  = local.fixture_internal_forward_uri
            timeoutInMillis      = var.integration_timeout_ms
            responseTransferMode = "STREAM"
            passthroughBehavior  = "when_no_match"
            connectionType       = "VPC_LINK"
            connectionId         = var.vpc_link_id
            integrationTarget    = var.fixture_alb_arn
            requestParameters = merge(
              {
                "integration.request.path.proxy" = "method.request.path.proxy"
              },
              local.fixture_verified_caller_identity,
            )
          }
        }
      }
      # -------------------------------------------------------------------
      # The human-session plane, auth NONE: the fixture gateway authenticates
      # these on their JWT. #5836 requires these routes to BLANK caller and
      # provenance, and that is a real requirement rather than defence in
      # depth: src/auth/dependencies.py skips the provenance branch only when
      # NO X-Caller-Identity is asserted, so a forwarded client header would
      # change which branch runs.
      # -------------------------------------------------------------------
      "/{proxy+}" = {
        x-amazon-apigateway-any-method = {
          "x-amazon-apigateway-auth" = { type = "NONE" }
          parameters = [
            {
              name     = "proxy"
              in       = "path"
              required = true
              type     = "string"
            }
          ]
          x-amazon-apigateway-integration = {
            type                 = "http_proxy"
            httpMethod           = "ANY"
            uri                  = "http://${var.fixture_alb_dns}/{proxy}"
            timeoutInMillis      = var.integration_timeout_ms
            responseTransferMode = "STREAM"
            passthroughBehavior  = "when_no_match"
            connectionType       = "VPC_LINK"
            connectionId         = var.vpc_link_id
            integrationTarget    = var.fixture_alb_arn
            requestParameters = merge(
              {
                "integration.request.path.proxy" = "method.request.path.proxy"
              },
              local.fixture_blank_caller_identity,
            )
          }
        }
      }
    }
    components = {
      securitySchemes = {
        sigv4 = {
          type                           = "apiKey"
          name                           = "Authorization"
          in                             = "header"
          "x-amazon-apigateway-authtype" = "awsSigv4"
        }
      }
    }
  })
}

resource "aws_api_gateway_rest_api" "fixture" {
  count = local.enabled ? 1 : 0

  name        = "${local.name_prefix}-fixture-edge"
  description = "DISPOSABLE Wave 2 fixture trusted edge (issue #5836, run ${var.run_nonce}). Safe to delete with the run."
  body        = local.fixture_api_body

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  tags = local.tags

  # The same invariant the ordinary module enforces (#5653), carried here rather
  # than inherited: a route added to THIS body later must also map both headers.
  # Asserted at plan time because the failure mode is a future edit, and it fails
  # silently — the apply succeeds and the header becomes forgeable on that path.
  # Reading self.body checks the ACTUALLY RENDERED document, so it cannot drift
  # from what is deployed.
  lifecycle {
    postcondition {
      condition = alltrue([
        for path_key, path_item in try(jsondecode(self.body).paths, {}) :
        alltrue([
          for required_header in [
            "integration.request.header.X-Caller-Identity",
            "integration.request.header.X-Adp-Edge-Provenance",
            ] : contains(
            keys(try(path_item["x-amazon-apigateway-any-method"]["x-amazon-apigateway-integration"].requestParameters, {})),
            required_header
          )
        ])
        if can(path_item["x-amazon-apigateway-any-method"]["x-amazon-apigateway-integration"])
      ])
      error_message = <<-EOT
        Issue #5836: every fixture-edge route must map BOTH caller identity and edge provenance.

        An unmapped header is forwarded from the client, and the gateway pod treats
        X-Caller-Identity as proof of identity — granting an unauthenticated caller
        a privileged internal TokenContext against the fixture.

        Add ONE of these to the new route's x-amazon-apigateway-integration:

          AWS_IAM route:  local.fixture_verified_caller_identity
          any other route: local.fixture_blank_caller_identity

        Do not remove this check to make a deploy pass.
      EOT
    }
  }
}

# =============================================================================
# Resource policy — the wrong-role refusal, without touching ordinary IAM
# =============================================================================
# LEAST PRIVILEGE, AND WHY NO IAM CHANGE IS NEEDED
# ------------------------------------------------
# The protected worker role's existing grants are wildcard-on-API-ID:
#   scaledjob-iam.tf:198-201        arn:aws:execute-api:us-east-1:*:*/*/*/internal/*
#   agent-authority-boundary.tf:11  arn:aws:execute-api:...:*/*/*/internal/v1/agent/*
# Because this fixture API serves that same /internal/v1/agent path, the worker
# can already invoke it. So supported access is achieved with ZERO widening of
# any ordinary permission — nothing is added to the identity policy or the
# permissions boundary, which #5836 requires.
#
# The consequence of that wildcard is that restriction must come from the API
# side, which is what this policy does: an explicit Deny for any principal other
# than the listed role ARNs. A valid signature from an unlisted role is refused
# at the edge, so "wrong-role calls are refused" is provable without weakening
# production.
resource "aws_api_gateway_rest_api_policy" "fixture" {
  count = local.enabled ? 1 : 0

  rest_api_id = aws_api_gateway_rest_api.fixture[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowListedFixtureCallers"
        Effect    = "Allow"
        Principal = { AWS = var.allowed_caller_role_arns }
        Action    = "execute-api:Invoke"
        Resource  = "${aws_api_gateway_rest_api.fixture[0].execution_arn}/*"
      },
      {
        # NotPrincipal + Deny is the only way to express "nobody else", and it
        # is evaluated before the Allow. Role ARNs are listed alongside their
        # assumed-role session form because a signature from an assumed session
        # presents the sts:assumed-role principal, not the iam:role one.
        Sid       = "DenyEveryoneElse"
        Effect    = "Deny"
        Principal = "*"
        Action    = "execute-api:Invoke"
        Resource  = "${aws_api_gateway_rest_api.fixture[0].execution_arn}/*"
        Condition = {
          StringNotLike = {
            "aws:PrincipalArn" = concat(
              var.allowed_caller_role_arns,
              [for arn in var.allowed_caller_role_arns :
                format(
                  "arn:aws:sts::%s:assumed-role/%s/*",
                  var.expected_account_id,
                  reverse(split("/", arn))[0]
                )
              ],
            )
          }
        }
      },
    ]
  })
}

resource "aws_api_gateway_deployment" "fixture" {
  count = local.enabled ? 1 : 0

  rest_api_id = aws_api_gateway_rest_api.fixture[0].id

  # The resource policy is part of the trigger deliberately: a policy change does
  # not take effect until the stage is redeployed. Without it here the apply
  # succeeds, the plan looks correct, and the wrong-role Deny is not actually in
  # force — an unrestricted trusted edge that reports as restricted.
  triggers = {
    redeployment = sha1(jsonencode([
      local.fixture_api_body,
      try(aws_api_gateway_rest_api_policy.fixture[0].policy, ""),
    ]))
  }

  depends_on = [aws_api_gateway_rest_api_policy.fixture]

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_cloudwatch_log_group" "fixture" {
  count = local.enabled ? 1 : 0

  name = "/aws/api-gateway/${local.name_prefix}-fixture-edge"

  # Short retention: this is disposable per-run evidence, and a log group that
  # outlives its run is an untracked leftover the cleanup ledger cannot reach.
  retention_in_days = 7
  tags              = local.tags
}

resource "aws_api_gateway_stage" "fixture" {
  count = local.enabled ? 1 : 0

  deployment_id = aws_api_gateway_deployment.fixture[0].id
  rest_api_id   = aws_api_gateway_rest_api.fixture[0].id
  stage_name    = var.environment

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.fixture[0].arn
    # Deliberately NO request/response headers in this format. The provenance
    # header would otherwise be written to CloudWatch, and #5836 requires the
    # secret to stay out of logs. caller identifies the verified principal,
    # which is the fact the evaluation needs, and is not a secret.
    format = jsonencode({
      requestId               = "$context.requestId"
      sourceIp                = "$context.identity.sourceIp"
      caller                  = "$context.identity.userArn"
      requestTime             = "$context.requestTime"
      httpMethod              = "$context.httpMethod"
      resourcePath            = "$context.resourcePath"
      status                  = "$context.status"
      integrationErrorMessage = "$context.integrationErrorMessage"
      integrationLatency      = "$context.integrationLatency"
    })
  }

  tags       = local.tags
  depends_on = [aws_cloudwatch_log_group.fixture]
}

# =============================================================================
# Provenance handoff to the fixture gateway
# =============================================================================
# The fixture pod must be configured with the SAME value this edge injects, or
# every internal call 403s. A SecureString parameter under a run-scoped path is
# the handoff: the fixture tooling reads it with --with-decryption when rendering
# the fixture ConfigMap, so the secret never transits an issue comment, a
# workflow log, or a published artifact.
#
# It is NOT a Terraform output (see outputs.tf): outputs land in state artifacts
# and console output, and #5836 requires secrets stay out of both.
resource "aws_ssm_parameter" "fixture_provenance_secret" {
  count = local.enabled ? 1 : 0

  name        = "/adp/${var.environment}/gateway/fixture/${var.run_nonce}/apigw-provenance-secret"
  description = "DISPOSABLE per-run fixture edge provenance proof (issue #5836, run ${var.run_nonce}). Not the ordinary gateway secret."
  type        = "SecureString"
  value       = random_password.fixture_edge_provenance[0].result

  tags = local.tags
}
