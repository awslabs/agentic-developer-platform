# =============================================================================
# WAFv2 Web ACL — Webhook Ingress
# =============================================================================
# Rate-based rule: 2000 requests per 5-minute window per source IP.
# Associated directly with the REST API stage (not possible with HTTP API v2).
#
# When the IP-set ARNs below are supplied, the ACL flips to default-Block with a
# single Allow rule over GitHub's published hooks ranges, internal callers and
# configured GitLab sources. Unset (the default) it stays default-Allow with the rate limit
# only, which is the prior behaviour.
#
# The internal-callers set is NOT optional when restricting. This ACL is
# associated with the whole stage, not one route, and the stage also serves
# POST /agent/trigger from the VPC's NAT egress address. Flipping the default to
# Block with only the GitHub ranges allowed would silently break agent chaining —
# the resource policy would permit the call and WAF would drop it first.
# =============================================================================

locals {
  waf_required_source_sets = compact([
    trimspace(var.github_hooks_ipv4_ip_set_arn),
    trimspace(var.github_hooks_ipv6_ip_set_arn),
    trimspace(var.internal_callers_ip_set_arn),
  ])
  waf_restrict = length(local.waf_required_source_sets) == 3
  waf_source_sets = distinct(concat(
    local.waf_required_source_sets,
    [for arn in var.gitlab_webhook_ip_set_arns : trimspace(arn)],
  ))

  # WAF log redaction does not cover sampled requests. Keep legacy sampling
  # only while logging is off; enabling logs must not create an unredacted copy.
  waf_sample_requests = !var.enable_waf_logging
  waf_redacted_headers = toset([
    "authorization", "cookie", "x-api-key", "x-amz-security-token", "x-gitlab-token",
  ])
}

resource "aws_wafv2_web_acl" "webhook" {
  name        = "${local.name_prefix}-webhook-ingress-waf"
  description = "Rate-limit webhook ingress to prevent abuse"
  scope       = "REGIONAL"

  lifecycle {
    # Resource preconditions work with the module's Terraform >= 1.5 contract;
    # cross-variable validation would require Terraform >= 1.9.
    precondition {
      condition     = contains([0, 3], length(local.waf_required_source_sets))
      error_message = "Supply all three WAF source IP-set ARNs (GitHub IPv4, GitHub IPv6 and internal callers), or leave all three empty. Partial source restriction is not allowed."
    }
    precondition {
      condition     = !local.waf_restrict || trimspace(var.github_hooks_ipv4_ip_set_arn) != trimspace(var.github_hooks_ipv6_ip_set_arn)
      error_message = "GitHub IPv4 and IPv6 must use different IP sets: a WAF IP set can contain only one address family."
    }
    precondition {
      condition     = !local.waf_restrict || !var.gitlab_webhook_enabled || length(var.gitlab_webhook_ip_set_arns) > 0
      error_message = "Restricting the shared webhook WAF while GitLab is enabled requires gitlab_webhook_ip_set_arns covering the GitLab server's outbound addresses. An existing internal-callers IP set may be reused if it covers those addresses."
    }
    precondition {
      condition     = length(var.gitlab_webhook_ip_set_arns) == 0 || (local.waf_restrict && var.gitlab_webhook_enabled)
      error_message = "GitLab WAF source sets require GitLab to be enabled and all three primary WAF source sets to be configured."
    }
    precondition {
      condition = alltrue([
        for arn in local.waf_source_sets :
        can(regex("^arn:(aws|aws-us-gov|aws-cn):wafv2:[a-z0-9-]+:[0-9]{12}:regional/ipset/[^/]+/[0-9a-fA-F-]{36}$", arn))
      ])
      error_message = "WAF source sets must be non-empty REGIONAL WAFv2 IP-set ARNs. CloudFront/global IP sets cannot be attached to this regional API stage."
    }
  }

  default_action {
    dynamic "allow" {
      for_each = local.waf_restrict ? [] : [1]
      content {}
    }
    dynamic "block" {
      for_each = local.waf_restrict ? [1] : []
      content {}
    }
  }

  # Allow is terminating. Evaluate the priority-1 rate-limit Block first so
  # even an allowlisted caller remains subject to throttling.
  dynamic "rule" {
    for_each = local.waf_restrict ? [1] : []
    content {
      name     = "allow-github-hooks-and-internal-callers"
      priority = 2

      action {
        allow {}
      }

      statement {
        or_statement {
          dynamic "statement" {
            for_each = local.waf_source_sets
            content {
              ip_set_reference_statement {
                arn = statement.value
              }
            }
          }
        }
      }

      visibility_config {
        sampled_requests_enabled   = local.waf_sample_requests
        cloudwatch_metrics_enabled = true
        metric_name                = "${local.name_prefix}-webhook-allow-known-sources"
      }
    }
  }

  rule {
    name     = "rate-limit-per-ip"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = 2000
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      sampled_requests_enabled   = local.waf_sample_requests
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name_prefix}-webhook-rate-limit"
    }
  }

  visibility_config {
    sampled_requests_enabled   = local.waf_sample_requests
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.name_prefix}-webhook-waf"
  }
}

resource "aws_wafv2_web_acl_association" "webhook" {
  resource_arn = aws_api_gateway_stage.dev.arn
  web_acl_arn  = aws_wafv2_web_acl.webhook.arn
}

# =============================================================================
# WAF logging
# =============================================================================
# The other two web ACLs in this deployment log to aws-waf-logs-* groups; this
# one logged nowhere, so a rate-limit block or an IP denial left no record at
# all. The destination name must begin with "aws-waf-logs-" — WAFv2 rejects any
# other log group name.
resource "aws_cloudwatch_log_group" "webhook_waf" {
  count = var.enable_waf_logging ? 1 : 0

  name              = "aws-waf-logs-${local.name_prefix}-webhook-ingress"
  retention_in_days = var.waf_log_retention_days
  kms_key_id        = aws_kms_key.cloudwatch.arn

  tags = {
    Name      = "aws-waf-logs-${local.name_prefix}-webhook-ingress"
    Component = "webhook-ingress"
  }
}

resource "aws_wafv2_web_acl_logging_configuration" "webhook" {
  count = var.enable_waf_logging ? 1 : 0

  resource_arn            = aws_wafv2_web_acl.webhook.arn
  log_destination_configs = [aws_cloudwatch_log_group.webhook_waf[0].arn]

  dynamic "redacted_fields" {
    for_each = local.waf_redacted_headers
    content {
      single_header {
        name = redacted_fields.value
      }
    }
  }
}
