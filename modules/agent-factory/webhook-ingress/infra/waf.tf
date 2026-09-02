# =============================================================================
# WAFv2 Web ACL — Webhook Ingress
# =============================================================================
# Rate-based rule: 2000 requests per 5-minute window per source IP.
# Associated directly with the REST API stage (not possible with HTTP API v2).
#
# When the IP-set ARNs below are supplied, the ACL flips to default-Block with a
# single Allow rule over GitHub's published hooks ranges plus the deployment's
# internal callers. Unset (the default) it stays default-Allow with the rate limit
# only, which is the prior behaviour.
#
# The internal-callers set is NOT optional when restricting. This ACL is
# associated with the whole stage, not one route, and the stage also serves
# POST /agent/trigger from the VPC's NAT egress address. Flipping the default to
# Block with only the GitHub ranges allowed would silently break agent chaining —
# the resource policy would permit the call and WAF would drop it first.
# =============================================================================

locals {
  # Restrict only when every set needed to avoid locking out a legitimate caller
  # is present. A partial configuration is worse than none: it would look
  # enforcing while blocking either GitHub or the agent path.
  waf_restrict = (
    var.github_hooks_ipv4_ip_set_arn != "" &&
    var.github_hooks_ipv6_ip_set_arn != "" &&
    var.internal_callers_ip_set_arn != ""
  )
}

resource "aws_wafv2_web_acl" "webhook" {
  name        = "${local.name_prefix}-webhook-ingress-waf"
  description = "Rate-limit webhook ingress to prevent abuse"
  scope       = "REGIONAL"

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

  # Priority 0 so it is evaluated before the rate limit: a legitimate GitHub
  # delivery that trips the rate limit should still be rate-limited, but an
  # address that is not allowed at all should never reach that rule.
  dynamic "rule" {
    for_each = local.waf_restrict ? [1] : []
    content {
      name     = "allow-github-hooks-and-internal-callers"
      priority = 0

      action {
        allow {}
      }

      statement {
        or_statement {
          statement {
            ip_set_reference_statement {
              arn = var.github_hooks_ipv4_ip_set_arn
            }
          }
          statement {
            ip_set_reference_statement {
              arn = var.github_hooks_ipv6_ip_set_arn
            }
          }
          statement {
            ip_set_reference_statement {
              arn = var.internal_callers_ip_set_arn
            }
          }
        }
      }

      visibility_config {
        sampled_requests_enabled   = true
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
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name_prefix}-webhook-rate-limit"
    }
  }

  visibility_config {
    sampled_requests_enabled   = true
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
}
