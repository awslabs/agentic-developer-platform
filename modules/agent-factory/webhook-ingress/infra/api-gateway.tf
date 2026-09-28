# =============================================================================
# API Gateway REST API v1
# =============================================================================
# REST API v1 chosen over HTTP API v2 to restore:
# - Direct WAFv2 association (rate-based rules, IP allowlist)
# - Per-method throttling (cap POST /github at 100 rps)
# - Resource policies (aws:SourceIp allowlist at the API layer)
#
# HTTP API v2's cost savings (~71%) are negligible at webhook-ingress volumes
# (low-frequency, GitHub retries on timeout). WAF + throttle > pennies saved.
# =============================================================================

resource "aws_api_gateway_rest_api" "webhook" {
  name        = "${local.name_prefix}-webhook-ingress"
  description = "Webhook ingress for hosted ADP - receives GitHub webhooks"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

# -----------------------------------------------------------------------------
# Resource policy
# -----------------------------------------------------------------------------
# Restrictions are expressed as *scoped explicit Denies* layered over the
# original blanket Allow, rather than as narrowed Allows. Two reasons:
#
#   1. Explicit Deny always wins in IAM evaluation, so adding a restriction can
#      never accidentally widen access — the failure mode of a narrowed-Allow
#      design, where a stray `Allow .../*` silently nullifies an IP condition.
#   2. Routes nobody has restricted keep working untouched, so this stays a
#      no-op for deployments that set neither variable.
#
# With both variables empty the statement list is byte-identical to the previous
# allow-all policy, so jsonencode produces the same string and the deployment
# trigger below does not fire.
#
# NOTE: `NotIpAddress` fails closed for address families absent from the list —
# a v4-only CIDR list denies IPv6 callers. That is the safe direction, but it is
# why github_webhook_source_cidrs must carry GitHub's v6 prefixes too.
# -----------------------------------------------------------------------------

locals {
  webhook_api_execution_arn = aws_api_gateway_rest_api.webhook.execution_arn

  webhook_policy_allow_all = [
    {
      Effect    = "Allow"
      Principal = "*"
      Action    = "execute-api:Invoke"
      Resource  = "${local.webhook_api_execution_arn}/*"
    }
  ]

  webhook_policy_deny_non_github = length(var.github_webhook_source_cidrs) > 0 ? [
    {
      Sid       = "DenyGitHubRouteOutsidePublishedRanges"
      Effect    = "Deny"
      Principal = "*"
      Action    = "execute-api:Invoke"
      Resource  = "${local.webhook_api_execution_arn}/*/POST/github"
      Condition = {
        NotIpAddress = { "aws:SourceIp" = var.github_webhook_source_cidrs }
      }
    }
  ] : []

  webhook_policy_deny_non_internal = length(var.internal_route_source_cidrs) > 0 ? [
    {
      Sid       = "DenyInternalRoutesOutsideAllowedSources"
      Effect    = "Deny"
      Principal = "*"
      Action    = "execute-api:Invoke"
      Resource  = "${local.webhook_api_execution_arn}/*/POST/agent/trigger"
      Condition = {
        NotIpAddress = { "aws:SourceIp" = var.internal_route_source_cidrs }
      }
    }
  ] : []

  webhook_policy_statements = concat(
    local.webhook_policy_allow_all,
    local.webhook_policy_deny_non_github,
    local.webhook_policy_deny_non_internal,
  )
}

resource "aws_api_gateway_rest_api_policy" "webhook" {
  rest_api_id = aws_api_gateway_rest_api.webhook.id

  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = local.webhook_policy_statements
  })
}

# -----------------------------------------------------------------------------
# Deployment + Stage
# -----------------------------------------------------------------------------

resource "aws_api_gateway_deployment" "webhook" {
  rest_api_id = aws_api_gateway_rest_api.webhook.id

  # Force redeployment when routes/integrations change
  # Issue #2152 review point I4: /agent/trigger IDs included so route is
  # deployed automatically on apply (not silently 403/404).
  # Issue #3324: /gitlab route IDs included for GitLab webhook.
  triggers = {
    redeployment = sha1(jsonencode(concat(
      [
        aws_api_gateway_resource.github.id,
        aws_api_gateway_method.post_github.id,
        aws_api_gateway_integration.github_webhook.id,
        aws_api_gateway_resource.agent.id,
        aws_api_gateway_resource.agent_trigger.id,
        aws_api_gateway_method.post_agent_trigger.id,
        aws_api_gateway_integration.agent_trigger.id,
        aws_api_gateway_rest_api_policy.webhook.policy,
      ],
      var.gitlab_webhook_enabled ? [
        aws_api_gateway_resource.gitlab[0].id,
        aws_api_gateway_method.post_gitlab[0].id,
        aws_api_gateway_integration.gitlab_webhook[0].id,
      ] : [],
    )))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_method.post_github,
    aws_api_gateway_integration.github_webhook,
    aws_api_gateway_method.post_agent_trigger,
    aws_api_gateway_integration.agent_trigger,
  ]

  # Note: GitLab resources (Issue #3324) are conditionally created and
  # referenced via the triggers hash above. depends_on cannot contain
  # conditional references, but the triggers hash ensures correct ordering.
}

resource "aws_api_gateway_stage" "dev" {
  deployment_id = aws_api_gateway_deployment.webhook.id
  rest_api_id   = aws_api_gateway_rest_api.webhook.id
  stage_name    = var.environment

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gateway.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      requestTime    = "$context.requestTime"
      httpMethod     = "$context.httpMethod"
      resourcePath   = "$context.resourcePath"
      status         = "$context.status"
      protocol       = "$context.protocol"
      responseLength = "$context.responseLength"
      integrationErr = "$context.integrationErrorMessage"
    })
  }
}

# -----------------------------------------------------------------------------
# Per-method throttling — cap POST /github to prevent one abusive caller from
# exhausting the account-level 10k rps quota.
# -----------------------------------------------------------------------------

resource "aws_api_gateway_method_settings" "throttle" {
  rest_api_id = aws_api_gateway_rest_api.webhook.id
  stage_name  = aws_api_gateway_stage.dev.stage_name
  method_path = "github/POST"

  settings {
    throttling_rate_limit  = 100
    throttling_burst_limit = 200
    logging_level          = "INFO"
    metrics_enabled        = true
  }
}

# Issue #2152: per-method throttling for POST /agent/trigger — tighter than
# /github (10 rps / 20 burst) since agent spawns are more expensive and
# unbounded spawn is a compute blowup risk.
resource "aws_api_gateway_method_settings" "throttle_agent_trigger" {
  rest_api_id = aws_api_gateway_rest_api.webhook.id
  stage_name  = aws_api_gateway_stage.dev.stage_name
  method_path = "agent/trigger/POST"

  settings {
    throttling_rate_limit  = 10
    throttling_burst_limit = 20
    logging_level          = "INFO"
    metrics_enabled        = true
  }
}

resource "aws_cloudwatch_log_group" "api_gateway" {
  #checkov:skip=CKV_AWS_338: Webhook API access logs use an explicitly bounded 14-day operational retention.
  name              = "/aws/apigateway/${local.name_prefix}-webhook-ingress"
  retention_in_days = 14
  kms_key_id        = aws_kms_key.cloudwatch.arn
}
