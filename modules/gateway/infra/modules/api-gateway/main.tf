# =============================================================================
# API Gateway REST API Module (Issue #236, Issue #260, Issue #42)
# =============================================================================
# Creates an API Gateway REST API with response streaming as an alternate route
# to the internal ALB. This provides 15-minute timeout support for long-running
# LLM requests (vs CloudFront's 60s hard limit).
#
# Architecture (Issue #42 — VPC Link v2 + ALB direct):
# Client -> API Gateway REST API (regional) -> VPC Link v2 -> ALB -> EKS
#
# The VPC Link v2 (apigatewayv2 namespace) connects directly to the ALB via
# subnets + security groups. No NLB is required. The ALB target is set at
# integration time via `--integration-target`, not at VPC Link creation time.
#
# Per: https://aws.amazon.com/blogs/compute/build-scalable-rest-apis-using-
# amazon-api-gateway-private-integration-with-application-load-balancer/
#
# This route coexists with the existing CloudFront -> VPC Origin -> ALB path.
# Clients choose which endpoint to use based on their needs.
#
# Issue #260: Dual-Path Architecture
# - /{proxy+}        -> NONE auth (humans with JWT -- FastAPI validates)
# - /agent/{proxy+}  -> AWS_IAM auth (agents with SigV4 -- API Gateway validates)
#
# NOTE on integrationTarget: Despite initial expectations, the OpenAPI
# `x-amazon-apigateway-integration` extension DOES accept `integrationTarget`
# when using a VPC Link v2. API Gateway requires it — put-rest-api rejects
# v2 VPC Link integrations that lack integrationTarget with the error:
# "IntegrationTarget is required for VpcLinkV2 <id>". This was discovered
# during deployment (see PR #46 description). Using inline integrationTarget
# is cleaner than a null_resource + local-exec post-deploy approach.
# =============================================================================

# =============================================================================
# VPC Link v2 for Private ALB Integration (Issue #42)
# =============================================================================
# Uses aws_apigatewayv2_vpc_link (v2 namespace) which takes subnets + SGs,
# NOT target_arns. This allows direct ALB integration without an NLB.
# The v2 VPC Link ID is referenced by the REST API (v1) integrations via
# connectionId, and the ALB is bound via --integration-target at integration
# creation time.

resource "aws_apigatewayv2_vpc_link" "main" {
  name               = "${var.name_prefix}-vpc-link-v2"
  subnet_ids         = var.private_subnet_ids
  security_group_ids = [aws_security_group.vpc_link.id]

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-vpc-link-v2"
    Service = "api-gateway"
    Purpose = "alb-integration-v2"
  })
}

# =============================================================================
# Security Group for VPC Link v2 (Issue #42)
# =============================================================================
# Egress to the ALB's security group on port 80 (HTTP).
# The ALB SG must allow inbound from this SG — handled by the ingress rule below.

locals {
  # Issue #4010 follow-up: on EKS Auto Mode both Ingress-managed ALBs share the
  # controller's frontend SG (observed live: sg-0623ec… appears in BOTH lists),
  # and that SG already carries the edge rules from alb_security_group_ids.
  # Emitting the same (peer, 80/tcp) rule again from the internal-plane blocks
  # fails the whole apply with InvalidPermission.Duplicate — which aborted the
  # 2026-08-26 gateway-infra-apply midway (run 33017530462). Only the SGs unique
  # to the internal-plane ALB need their own rules.
  internal_plane_only_sg_ids = [
    for sg in var.internal_plane_alb_security_group_ids : sg
    if !contains(var.alb_security_group_ids, sg)
  ]
}

resource "aws_security_group" "vpc_link" {
  name_prefix = "${var.name_prefix}-vpc-link-v2-"
  description = "API Gateway VPC Link v2 to ALB (Issue #42)"
  vpc_id      = var.vpc_id

  # Egress to ALB SG(s) — only created when ALB SG IDs are provided.
  # On initial deploy (before ALB exists), alb_security_group_ids is []
  # and no egress rules are created. The deploy workflow adds the SG IDs
  # once the EKS Ingress ALB is provisioned.
  dynamic "egress" {
    for_each = length(var.alb_security_group_ids) > 0 ? [1] : []
    content {
      description     = "Allow VPC Link to reach ALB on port 80"
      from_port       = 80
      to_port         = 80
      protocol        = "tcp"
      security_groups = var.alb_security_group_ids
    }
  }

  # Issue #4010: egress to the internal-plane ALB's SG(s). The
  # `/internal/{proxy+}` route targets that ALB, so without this the VPC Link
  # cannot open the connection and the route times out (~10s) then 503s.
  # Empty until wire-gateway-alb.sh discovers the internal Ingress's ALB.
  dynamic "egress" {
    for_each = length(local.internal_plane_only_sg_ids) > 0 ? [1] : []
    content {
      description     = "Allow VPC Link to reach internal-plane ALB on port 80 (Issue #4010)"
      from_port       = 80
      to_port         = 80
      protocol        = "tcp"
      security_groups = local.internal_plane_only_sg_ids
    }
  }

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-vpc-link-v2-sg"
    Service = "api-gateway"
    Purpose = "vpc-link-v2-security"
  })
}

# Allow ALB to accept inbound traffic from the VPC Link SG
resource "aws_security_group_rule" "alb_from_vpc_link" {
  count = length(var.alb_security_group_ids)

  description              = "Allow inbound from API Gateway VPC Link v2 (Issue #42)"
  type                     = "ingress"
  from_port                = 80
  to_port                  = 80
  protocol                 = "tcp"
  security_group_id        = var.alb_security_group_ids[count.index]
  source_security_group_id = aws_security_group.vpc_link.id
}

# Issue #4010: the matching inbound half on the internal-plane ALB's SG(s).
# Both directions are required — the spike confirmed that opening only one side
# leaves the connection silently dropped rather than refused.
resource "aws_security_group_rule" "internal_plane_alb_from_vpc_link" {
  count = length(local.internal_plane_only_sg_ids)

  description              = "Allow inbound from API Gateway VPC Link v2 to internal plane (Issue #4010)"
  type                     = "ingress"
  from_port                = 80
  to_port                  = 80
  protocol                 = "tcp"
  security_group_id        = local.internal_plane_only_sg_ids[count.index]
  source_security_group_id = aws_security_group.vpc_link.id
}

# =============================================================================
# API Gateway REST API (Regional) -- OpenAPI Definition
# =============================================================================
# Using Swagger 2.0 body to set responseTransferMode: STREAM on integrations
# and proper AWS_IAM auth via x-amazon-apigateway-auth at method level.
#
# Issue #42: integrationTarget (ALB ARN) IS supported in the OpenAPI body
# when using VPC Link v2. API Gateway requires it -- put-rest-api rejects
# v2 VPC Link integrations that lack integrationTarget. This was discovered
# during deployment when the body without integrationTarget was rejected with:
# "IntegrationTarget is required for VpcLinkV2 <id>"

# =============================================================================
# Internal-plane routing target (Issue #4010)
# =============================================================================
# The `/internal/{proxy+}` route points at a dedicated internal-plane ALB so the
# internal control plane is not reachable through the ALB that CloudFront fronts.
# When the internal-plane vars are empty (pre-#4010, or before the internal
# Ingress has materialized its ALB) these fall back to the edge ALB, preserving
# exactly the previous behavior. That fallback is what makes this change safe to
# merge ahead of the cluster-side rollout.
locals {
  internal_plane_alb_dns = var.internal_plane_alb_dns != "" ? var.internal_plane_alb_dns : var.internal_alb_dns
  internal_plane_alb_arn = var.internal_plane_alb_arn != "" ? var.internal_plane_alb_arn : var.internal_alb_arn
}

resource "random_password" "edge_provenance" {
  length  = 64
  special = false
}

# Issue #5653 (A01): the identity header must never be forwarded from the client.
#
# X-Caller-Identity is proof of identity to the gateway pod: it names an IAM
# principal, and the pod resolves it against the agent registry to a privileged
# TokenContext. That is only sound when API GATEWAY wrote the value, which it does
# on the AWS_IAM routes via `context.identity.userArn` — a value taken from the
# verified SigV4 signature that a client cannot influence.
#
# AWS_IAM routes also inject an independently generated edge proof. The pod
# validates that proof before trusting the identity, so a direct-cluster caller
# cannot bypass API Gateway by supplying only the identity header. Auth-NONE
# routes blank both headers, replacing any inbound values.
#
# Blank (not absent) is deliberate — an integration request parameter can only be
# mapped to a value, not dropped, and the pod treats empty as "no identity"
# (`headers.get(...).strip()` is falsy), so blank and absent are equivalent to the
# application while blank is what API Gateway can actually guarantee.
#
# 'single-quoted' is API Gateway mapping syntax for a static string literal.
locals {
  blank_caller_identity = {
    "integration.request.header.X-Caller-Identity"     = "''"
    "integration.request.header.X-Adp-Edge-Provenance" = "''"
  }
  verified_caller_identity = {
    "integration.request.header.X-Caller-Identity"     = "context.identity.userArn"
    "integration.request.header.X-Adp-Edge-Provenance" = "'${random_password.edge_provenance.result}'"
  }
}

resource "aws_api_gateway_rest_api" "main" {
  name        = "${var.name_prefix}-api"
  description = "Bedrock Gateway REST API with response streaming (Issue #236)"

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  # Issue #260: Dual-path architecture
  # - /{proxy+}        -> NONE auth (humans with JWT -- FastAPI validates)
  # - /agent/{proxy+}  -> AWS_IAM auth (agents with SigV4 -- API Gateway validates)
  #
  # Issue #42: Uses Swagger 2.0 for proper securityDefinitions + x-amazon-apigateway-auth.
  # VPC Link v2 connectionId + integrationTarget set in each integration block.
  body = var.internal_alb_dns != "localhost" && var.internal_alb_dns != "" ? jsonencode({
    swagger = "2.0"
    info = {
      title       = "${var.name_prefix}-api"
      version     = "3.0"
      description = "Issue #42: VPC Link v2 + ALB direct, dual-path auth"
    }
    # Swagger 2.0: securityDefinitions for AWS_IAM (SigV4)
    securityDefinitions = {
      sigv4 = {
        type                           = "apiKey"
        name                           = "Authorization"
        in                             = "header"
        "x-amazon-apigateway-authtype" = "awsSigv4"
      }
    }
    paths = merge({
      # Root path - NONE auth (humans)
      "/" = {
        x-amazon-apigateway-any-method = {
          "x-amazon-apigateway-auth" = { type = "NONE" }
          x-amazon-apigateway-integration = {
            type                 = "http_proxy"
            httpMethod           = "ANY"
            uri                  = "http://${var.internal_alb_dns}/"
            timeoutInMillis      = var.integration_timeout_ms
            responseTransferMode = "STREAM"
            passthroughBehavior  = "when_no_match"
            connectionType       = "VPC_LINK"
            connectionId         = aws_apigatewayv2_vpc_link.main.id
            integrationTarget    = var.internal_alb_arn
            # Issue #5653: BLANK the identity header on this NONE-auth route.
            # See the local.blank_caller_identity rationale above.
            requestParameters = local.blank_caller_identity
          }
        }
      }
      # Proxy path - NONE auth (humans)
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
            uri                  = "http://${var.internal_alb_dns}/{proxy}"
            timeoutInMillis      = var.integration_timeout_ms
            responseTransferMode = "STREAM"
            passthroughBehavior  = "when_no_match"
            connectionType       = "VPC_LINK"
            connectionId         = aws_apigatewayv2_vpc_link.main.id
            integrationTarget    = var.internal_alb_arn
            # Issue #5653: BLANK the identity header. This route is auth NONE —
            # API Gateway verifies no signature here — so a client-supplied
            # X-Caller-Identity would otherwise be forwarded to the pod verbatim
            # and honoured as proof of identity. CloudFront deletes the header,
            # but the API Gateway invoke URL is directly reachable, so the edge
            # function is not the only way in. Blanking it here means the value
            # the pod sees on this route is always empty, whatever the client sent.
            requestParameters = merge(
              {
                "integration.request.path.proxy" = "method.request.path.proxy"
              },
              local.blank_caller_identity,
            )
            cacheKeyParameters = ["method.request.path.proxy"]
          }
        }
      }
      # Issue #260: Agent root path - AWS_IAM auth (agents)
      "/agent" = {
        x-amazon-apigateway-any-method = {
          security                   = [{ sigv4 = [] }]
          "x-amazon-apigateway-auth" = { type = "AWS_IAM" }
          x-amazon-apigateway-integration = {
            type                 = "http_proxy"
            httpMethod           = "ANY"
            uri                  = "http://${var.internal_alb_dns}/"
            timeoutInMillis      = var.integration_timeout_ms
            responseTransferMode = "STREAM"
            passthroughBehavior  = "when_no_match"
            connectionType       = "VPC_LINK"
            connectionId         = aws_apigatewayv2_vpc_link.main.id
            integrationTarget    = var.internal_alb_arn
            requestParameters    = local.verified_caller_identity
          }
        }
      }
      # Issue #260: Agent proxy path - AWS_IAM auth (agents)
      "/agent/{proxy+}" = {
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
            uri                  = "http://${var.internal_alb_dns}/{proxy}"
            timeoutInMillis      = var.integration_timeout_ms
            responseTransferMode = "STREAM"
            passthroughBehavior  = "when_no_match"
            connectionType       = "VPC_LINK"
            connectionId         = aws_apigatewayv2_vpc_link.main.id
            integrationTarget    = var.internal_alb_arn
            requestParameters = merge(
              {
                "integration.request.path.proxy" = "method.request.path.proxy"
              },
              local.verified_caller_identity,
            )
            cacheKeyParameters = ["method.request.path.proxy"]
          }
        }
      }
      # Issue #1108: Internal platform route — AWS_IAM auth (deploy-runner)
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
            type       = "http_proxy"
            httpMethod = "ANY"
            # Preserve the /internal prefix when forwarding to the gateway pod.
            # The Bedrock /agent proxy strips its prefix because the pod serves
            # Bedrock requests at root paths; but the /internal/v1/* routes are
            # registered with the prefix included, so 404s without it.
            # Issue #4010: routed to the dedicated internal-plane ALB (which
            # CloudFront has no VPC origin for), falling back to the edge ALB
            # while internal_plane_alb_* are unset. integrationTarget must be a
            # LOAD BALANCER ARN — a listener ARN is rejected by the API.
            uri                  = "http://${local.internal_plane_alb_dns}/internal/{proxy}"
            timeoutInMillis      = var.integration_timeout_ms
            responseTransferMode = "STREAM"
            passthroughBehavior  = "when_no_match"
            connectionType       = "VPC_LINK"
            connectionId         = aws_apigatewayv2_vpc_link.main.id
            integrationTarget    = local.internal_plane_alb_arn
            requestParameters = merge(
              {
                "integration.request.path.proxy" = "method.request.path.proxy"
              },
              local.verified_caller_identity,
            )
            cacheKeyParameters = ["method.request.path.proxy"]
          }
        }
      }
      },
      # Issue #1011: GitHub Auth Broker route — Lambda proxy integration
      # Only included when broker_lambda_invoke_arn is provided.
      var.broker_lambda_invoke_arn != "" ? {
        "/auth/github/{proxy+}" = {
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
              type                = "aws_proxy"
              httpMethod          = "POST"
              uri                 = var.broker_lambda_invoke_arn
              passthroughBehavior = "when_no_match"
              contentHandling     = "CONVERT_TO_TEXT"
              timeoutInMillis     = 29000
              # Issue #5653: BLANK the identity header on this NONE-auth route
              # too. The broker Lambda does not consume X-Caller-Identity today,
              # but leaving a client-settable identity header flowing into any
              # auth-NONE integration is the pattern this issue exists to remove,
              # and the invariant test below asserts it holds for every route.
              requestParameters = local.blank_caller_identity
            }
          }
        }
      } : {}
    )
    }) : jsonencode({
    swagger = "2.0"
    info = {
      title   = "${var.name_prefix}-api"
      version = "1.0"
    }
    paths = {
      "/status" = {
        get = {
          x-amazon-apigateway-integration = {
            type = "MOCK"
            requestTemplates = {
              "application/json" = "{\"statusCode\": 200}"
            }
            responses = {
              default = {
                statusCode = "200"
                responseTemplates = {
                  "application/json" = "{\"status\":\"awaiting-backend\",\"message\":\"API Gateway created. Backend ALB not yet configured.\"}"
                }
              }
            }
          }
        }
      }
    }
  })

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-api-gateway"
    Service = "api-gateway"
    Purpose = "llm-streaming-alternate-route"
  })

  # ===========================================================================
  # Issue #5653 (A01): the route invariant, asserted at plan time
  # ===========================================================================
  # Every route must either SET X-Caller-Identity from the verified SigV4
  # identity, or BLANK it. No route may leave it unmapped, because unmapped
  # means "forward whatever the client sent" — and the pod treats that header
  # as proof of identity, resolving it against the agent registry to a
  # privileged TokenContext.
  #
  # This is asserted here rather than only in a test because the failure mode is
  # a route ADDED LATER. The blanking on today's five routes is easy to review;
  # what is not easy is remembering, months from now, that a new auth-NONE path
  # added to this same `paths` map silently reopens an unauthenticated path to
  # platform-scope authority. A plan-time postcondition makes that omission fail
  # the deploy that introduces it, at the moment it is introduced, instead of
  # depending on a reviewer noticing an absent line.
  #
  # Reading `self.body` checks the ACTUAL rendered document — after the
  # conditionals and merges — so it cannot drift from what is deployed the way a
  # parallel list of expected paths would.
  lifecycle {
    postcondition {
      # The MOCK placeholder body (no ALB yet) has no integrations to the pod at
      # all, so the invariant is vacuous there and the check is skipped.
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
        Issue #5653: every API Gateway route must map both caller identity and edge provenance headers.

        A route that does not map it forwards the client's value to the gateway pod,
        which treats X-Caller-Identity as proof of identity and resolves it against
        the agent registry — granting an unauthenticated caller a privileged
        internal/platform TokenContext.

        Add ONE of the following to the new route's x-amazon-apigateway-integration:

          AWS_IAM route (API Gateway verified a SigV4 signature):
            requestParameters = local.verified_caller_identity

          any other route (auth NONE, Lambda proxy, etc.):
            requestParameters = local.blank_caller_identity

        Do not remove this check to make a deploy pass.
      EOT
    }
  }
}

# =============================================================================
# CloudWatch Log Group for Access Logging
# =============================================================================

resource "aws_cloudwatch_log_group" "api_gateway" {
  name              = "/aws/api-gateway/${var.name_prefix}-api"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.cloudwatch_kms_key_arn

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-api-gateway-logs"
    Service = "cloudwatch"
    Purpose = "api-gateway-access-logs"
  })
}

# =============================================================================
# API Gateway Resource Policy — per-path source restrictions
# =============================================================================
# Created only when at least one CIDR list is populated, so a deployment that
# sets neither has no resource policy and behaves exactly as before.
#
# Shape: one blanket Allow, then scoped explicit Denies. Not a narrowed Allow —
# an explicit Deny always wins, so the restriction cannot be nullified by a
# broader Allow appearing later in the same policy. Same construction as the
# webhook API's policy.
#
# The Denies are per-path on purpose. `/auth/github/*` is excluded because
# CloudFront proxies it here from edge addresses, which are neither a browser's
# nor the NAT's and cannot be expressed in a resource policy — API Gateway does
# not support managed prefix lists. Its EAA restriction is applied by the
# CloudFront web ACL instead. `/{proxy+}` and `/status` are excluded because
# their callers are not enumerated; restricting them is a separate decision.
#
# `/agent` is listed as well as `/agent/*`: the sigv4 proxy target is
# <invoke_url>/agent with no trailing segment, so a policy covering only
# /agent/* would miss the calls that matter most.

locals {
  api_policy_enabled = length(var.agent_route_source_cidrs) > 0 || length(var.internal_route_source_cidrs) > 0

  api_policy_statements = concat(
    [
      {
        Sid       = "AllowInvokeByDefault"
        Effect    = "Allow"
        Principal = "*"
        Action    = "execute-api:Invoke"
        Resource  = "${aws_api_gateway_rest_api.main.execution_arn}/*"
      }
    ],
    length(var.agent_route_source_cidrs) > 0 ? [
      {
        Sid       = "DenyAgentRoutesOutsideAllowedSources"
        Effect    = "Deny"
        Principal = "*"
        Action    = "execute-api:Invoke"
        Resource = [
          "${aws_api_gateway_rest_api.main.execution_arn}/*/*/agent",
          "${aws_api_gateway_rest_api.main.execution_arn}/*/*/agent/*",
        ]
        Condition = {
          NotIpAddress = { "aws:SourceIp" = var.agent_route_source_cidrs }
        }
      }
    ] : [],
    length(var.internal_route_source_cidrs) > 0 ? [
      {
        Sid       = "DenyInternalRoutesOutsideAllowedSources"
        Effect    = "Deny"
        Principal = "*"
        Action    = "execute-api:Invoke"
        Resource  = "${aws_api_gateway_rest_api.main.execution_arn}/*/*/internal/*"
        Condition = {
          NotIpAddress = { "aws:SourceIp" = var.internal_route_source_cidrs }
        }
      }
    ] : [],
  )
}

resource "aws_api_gateway_rest_api_policy" "main" {
  count = local.api_policy_enabled ? 1 : 0

  rest_api_id = aws_api_gateway_rest_api.main.id

  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = local.api_policy_statements
  })
}

# =============================================================================
# API Gateway Deployment
# =============================================================================

resource "aws_api_gateway_deployment" "main" {
  rest_api_id = aws_api_gateway_rest_api.main.id

  # The policy is part of the trigger deliberately. A resource-policy change does
  # not take effect until the stage is redeployed, and without it here the apply
  # succeeds, the plan looks right, and nothing is actually restricted — the
  # failure mode the runbook flags for the console revert path.
  triggers = {
    # jsonencode the statements to a string before the conditional: a ternary
    # requires both branches to unify, and a populated tuple will not unify with
    # an empty one. Two strings always do.
    redeployment = sha1(jsonencode([
      coalesce(aws_api_gateway_rest_api.main.body, "initial"),
      local.api_policy_enabled ? jsonencode(local.api_policy_statements) : "",
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }
}

# =============================================================================
# API Gateway Stage
# =============================================================================

resource "aws_api_gateway_stage" "main" {
  deployment_id = aws_api_gateway_deployment.main.id
  rest_api_id   = aws_api_gateway_rest_api.main.id
  stage_name    = var.environment

  xray_tracing_enabled = var.enable_xray_tracing

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gateway.arn
    format = jsonencode({
      requestId               = "$context.requestId"
      sourceIp                = "$context.identity.sourceIp"
      requestTime             = "$context.requestTime"
      protocol                = "$context.protocol"
      httpMethod              = "$context.httpMethod"
      resourcePath            = "$context.resourcePath"
      status                  = "$context.status"
      responseLength          = "$context.responseLength"
      integrationErrorMessage = "$context.integrationErrorMessage"
      integrationLatency      = "$context.integrationLatency"
      responseLatency         = "$context.responseLatency"
    })
  }

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-api-gateway-stage"
    Service = "api-gateway"
    Purpose = "deployment-stage"
  })

  depends_on = [aws_cloudwatch_log_group.api_gateway]
}

# =============================================================================
# API Gateway Method Settings (Throttling and Metrics)
# =============================================================================

resource "aws_api_gateway_method_settings" "all" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  stage_name  = aws_api_gateway_stage.main.stage_name
  method_path = "*/*"

  settings {
    metrics_enabled = true
    logging_level   = "INFO"

    # Issue #5672. This was `var.environment != "prod"`, i.e. full request/response
    # payload tracing was ON in every environment whose name was not literally
    # "prod". data_trace_enabled writes complete requests and responses — headers
    # included — into the CloudWatch log group. Headers are where callers present
    # their bearer tokens and the internal-plane shared secret; bodies are the
    # prompts and completions. That put replayable credentials and private user
    # content in front of everyone with log read access: CI roles, build roles,
    # any operator.
    #
    # Deriving it from the environment NAME is the part that made this durable: a
    # new environment is exposed by default because nobody added its name to a
    # comparison. Now it is an explicit input, default false, set true nowhere.
    # Turning payload tracing on has to be a deliberate, reviewed, per-environment
    # act with a plan diff that shows it.
    #
    # The access_log_settings format on the stage above stays the single sanctioned
    # gateway log source: request id, source IP, time, method, path, status, length
    # and latencies — metadata only, no headers and no bodies.
    data_trace_enabled = var.enable_payload_tracing

    throttling_burst_limit = var.throttle_burst_limit
    throttling_rate_limit  = var.throttle_rate_limit

    caching_enabled = false
  }
}

# =============================================================================
# IAM Role for API Gateway CloudWatch Logging
# =============================================================================

data "aws_iam_policy_document" "api_gateway_assume_role" {
  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["apigateway.amazonaws.com"]
    }
    actions = ["sts:AssumeRole"]
  }
}

resource "aws_iam_role" "api_gateway_cloudwatch" {
  permissions_boundary = var.automation_permissions_boundary_arn
  name                 = "${var.name_prefix}-api-gateway-cloudwatch"
  assume_role_policy   = data.aws_iam_policy_document.api_gateway_assume_role.json

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-api-gateway-cloudwatch-role"
    Service = "iam"
    Purpose = "api-gateway-logging"
  })
}

resource "aws_iam_role_policy_attachment" "api_gateway_cloudwatch" {
  role       = aws_iam_role.api_gateway_cloudwatch.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

resource "aws_api_gateway_account" "main" {
  cloudwatch_role_arn = aws_iam_role.api_gateway_cloudwatch.arn
  depends_on          = [aws_iam_role_policy_attachment.api_gateway_cloudwatch]
}

# =============================================================================
# GitHub Auth Broker Lambda Permission (Issue #1011)
# =============================================================================
# Allows API Gateway to invoke the broker Lambda for /auth/github/* routes.

resource "aws_lambda_permission" "broker_api_gateway" {
  # Use the plan-time-known enable flag, NOT broker_lambda_invoke_arn — the
  # latter is the broker Lambda's computed invoke ARN (unknown until apply),
  # which makes count un-evaluable at plan time ("Invalid count argument").
  count = var.enable_broker_route ? 1 : 0

  statement_id  = "AllowAPIGatewayInvokeBroker"
  action        = "lambda:InvokeFunction"
  function_name = var.broker_lambda_function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.main.execution_arn}/*/*"
}

# =============================================================================
# Edge provenance signal for the ConfigMap renderers (Issue #5653, A01)
# =============================================================================
# BG_TRUST_APIGW_HEADERS tells the gateway pod "an X-Caller-Identity that
# reaches you was written by API Gateway from a verified SigV4 signature, so you
# may believe it". That claim is only true once the blanking above is deployed —
# it is a statement about THIS module's state.
#
# Until now both ConfigMap renderers hard-coded it to `true`, which meant the
# application's safe default (trust_apigw_headers = False in src/shared/config.py)
# was overridden on every deploy regardless of whether any edge control existed.
# The flag asserted a property nothing had established.
#
# Publishing it from the same module that installs the blanking couples the two:
# the renderers read this param, so the pod believes the header only in an
# environment whose edge actually blanks it. An environment that has not applied
# this module has no param, the renderers fall back to "false", and forged
# assertions are inert rather than authoritative.
#
# Rollout ordering (important, and the reason this is a param and not a literal):
# apply this module BEFORE rolling out the app build that reads the param. In the
# reverse order the pod is merely stricter than necessary for one rollout —
# vouched agent traffic is refused until the param exists, which is an availability
# regression, not a security one. The dangerous order is the opposite one, and it
# is now impossible: the flag cannot be true without the blanking.
resource "aws_ssm_parameter" "trust_apigw_headers" {
  name        = "/adp/${var.environment}/gateway/trust-apigw-headers"
  description = "Whether the gateway may evaluate API Gateway identity headers. Identity still requires the SecureString-backed edge proof. Issue #5653."
  type        = "String"
  value       = "true"

  tags = var.common_tags

  # Operators need a break-glass: if the tightening rejects a caller nobody
  # anticipated, set this to "false" (SSM put + rollout restart) to make the
  # header inert while the cause is diagnosed. Terraform must not revert that on
  # the next apply. Matches the budget-fail-mode lever in gateway/infra/main.tf.
  lifecycle {
    ignore_changes = [value]
  }

  # The param is a claim about the blanking, so it must not exist before the
  # route table that does the blanking.
  depends_on = [aws_api_gateway_rest_api.main]
}

resource "aws_ssm_parameter" "edge_provenance_secret" {
  name        = "/adp/${var.environment}/gateway/apigw-provenance-secret"
  description = "Shared proof injected only by API Gateway AWS_IAM integrations and validated by the gateway pod. Issue #5653."
  type        = "SecureString"
  value       = random_password.edge_provenance.result

  tags = var.common_tags
}
