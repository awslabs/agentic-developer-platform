# =============================================================================
# WAFv2 Web ACL — Webhook Ingress
# =============================================================================
# Rate-based rule: 2000 requests per 5-minute window per source IP.
# Associated directly with the REST API stage (not possible with HTTP API v2).
#
# Follow-up: add IP-set rule restricting to GitHub's published meta/hooks CIDRs.
# =============================================================================

resource "aws_wafv2_web_acl" "webhook" {
  name        = "${local.name_prefix}-webhook-ingress-waf"
  description = "Rate-limit webhook ingress to prevent abuse"
  scope       = "REGIONAL"

  default_action {
    allow {}
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
