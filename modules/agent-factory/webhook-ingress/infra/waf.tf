# =============================================================================
# WAF WebACL
# =============================================================================
# Rate-limit rule protects the webhook endpoint from abuse.
# Geo-restrict placeholder for future use.
# =============================================================================

resource "aws_wafv2_web_acl" "webhook" {
  name        = "${local.name_prefix}-webhook-waf"
  description = "WAF for webhook ingress API Gateway"
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
        limit              = var.waf_rate_limit
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name_prefix}-webhook-rate-limit"
    }
  }

  # Geo-restrict placeholder — uncomment to block specific countries
  # rule {
  #   name     = "geo-restrict"
  #   priority = 2
  #
  #   action {
  #     block {}
  #   }
  #
  #   statement {
  #     geo_match_statement {
  #       country_codes = ["XX"]
  #     }
  #   }
  #
  #   visibility_config {
  #     sampled_requests_enabled   = true
  #     cloudwatch_metrics_enabled = true
  #     metric_name                = "${local.name_prefix}-webhook-geo-block"
  #   }
  # }

  visibility_config {
    sampled_requests_enabled   = true
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.name_prefix}-webhook-waf"
  }
}

resource "aws_wafv2_web_acl_association" "webhook" {
  resource_arn = aws_apigatewayv2_stage.default.arn
  web_acl_arn  = aws_wafv2_web_acl.webhook.arn
}
