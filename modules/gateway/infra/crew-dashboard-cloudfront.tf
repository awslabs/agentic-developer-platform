# Crew dashboard edge, CloudFront mode (#7902).
#
# Serves the Crew dashboard on a dedicated distribution's own *.cloudfront.net
# name, so an account without a public Route 53 zone can enable Crew. Nothing
# here needs a certificate, a DNS record or a public load balancer: the
# distribution reuses the frontend's VPC origin (modules/cloudfront,
# aws_cloudfront_vpc_origin.api) to the existing *internal* gateway ALB.
#
# Routing assumption (verified against modules/gateway/k8s/ingress.yaml at
# e305019b0): the gateway Ingress rules are path-only — no `host` — so the ALB
# forwards every Host to `bedrockgateway:80` on `/` and the gateway's
# `Host(origin.hostname)` route (src/crew/dashboard.py, install_crew_dashboard)
# selects the Crew proxy by the forwarded Host header. The same manifest sends
# `/tools*`, `/agent/tools*`, `/api/tools*` and `/api/agent/tools*` to
# `tools-facade` for every Host; none of these are Crew dashboard paths.
#
# Trade-off: CloudFront caps a VPC origin's read timeout at 60 seconds, so a
# stream that stays silent longer than that is cut. The Crew proxy's keepalive
# (src/crew/keepalive.py, CREW_PROXY_KEEPALIVE_SECONDS, default 25) sends an SSE
# comment or lets the protocol layer ping the browser well inside that window,
# so idle sessions survive. ALB mode (crew-dashboard.tf) remains for accounts
# with a public zone and no CloudFront in the Crew path.
#
# Policies: the frontend distribution's `/api/*` behaviour already reaches this
# same VPC origin with Managed-AllViewer (which forwards Host) and
# Managed-CachingDisabled and is live in dev, so the pair is proven for an ALB
# VPC origin; API Gateway is the only origin type in this stack that needs the
# except-Host variant. No response-headers policy is attached: the frontend's
# CSP / frame-ancestors would break the Kiro Crew dashboard, which sets its own.
# `compress = false` keeps CloudFront from buffering streamed bodies.

locals {
  crew_cloudfront_origin_id = "${local.name_prefix}-crew-vpc-origin"
}

data "aws_cloudfront_cache_policy" "crew_caching_disabled" {
  count = local.crew_dashboard_cloudfront ? 1 : 0
  name  = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "crew_all_viewer" {
  count = local.crew_dashboard_cloudfront ? 1 : 0
  name  = "Managed-AllViewer"
}

resource "aws_cloudfront_distribution" "crew_dashboard" {
  count = local.crew_dashboard_cloudfront ? 1 : 0

  lifecycle {
    precondition {
      condition     = var.crew_dashboard_hostname == "" && var.crew_dashboard_zone_id == ""
      error_message = "crew_dashboard_edge = \"cloudfront\" serves Crew on the distribution's own *.cloudfront.net name: leave crew_dashboard_hostname and crew_dashboard_zone_id empty, or use crew_dashboard_edge = \"alb\" for a public zone."
    }
    precondition {
      condition     = var.enable_vpc_origin && var.internal_alb_arn != "" && var.internal_alb_dns != "" && var.internal_alb_dns != "localhost"
      error_message = "The CloudFront Crew edge reuses the frontend's VPC origin to the internal gateway ALB: apply with enable_vpc_origin, internal_alb_arn and internal_alb_dns set (the wire-gateway-alb.sh / gateway-infra-apply.yml second pass) before enabling it."
    }
  }

  enabled         = true
  is_ipv6_enabled = var.cloudfront_enable_ipv6
  comment         = "${local.name_prefix} Crew dashboard edge (Host-routed to the gateway)"
  http_version    = "http2"
  # Same class the frontend module derives for this environment.
  price_class = var.environment == "prod" ? "PriceClass_All" : "PriceClass_100"

  origin {
    # The ALB DNS name is required here even for a VPC origin (the API rejects an
    # ARN-shaped domain_name); routing is enforced by vpc_origin_config.
    domain_name = var.internal_alb_dns
    origin_id   = local.crew_cloudfront_origin_id

    vpc_origin_config {
      vpc_origin_id            = module.cloudfront.vpc_origin_id
      origin_read_timeout      = 60
      origin_keepalive_timeout = 60
    }
  }

  default_cache_behavior {
    allowed_methods  = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods   = ["GET", "HEAD"]
    target_origin_id = local.crew_cloudfront_origin_id

    viewer_protocol_policy   = "https-only"
    cache_policy_id          = data.aws_cloudfront_cache_policy.crew_caching_disabled[0].id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.crew_all_viewer[0].id
    compress                 = false
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  dynamic "logging_config" {
    for_each = var.enable_cloudfront_logging ? [1] : []
    content {
      bucket          = module.s3_cloudfront_logs[0].bucket_domain_name
      prefix          = "crew-cloudfront-logs/"
      include_cookies = false
    }
  }

  # Only when the frontend distribution is also behind a web ACL.
  web_acl_id = var.cloudfront_waf_web_acl_arn != "" ? var.cloudfront_waf_web_acl_arn : null

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-crew-cloudfront"
    Service = "cdn"
    Purpose = "crew-dashboard-edge"
  })
}
