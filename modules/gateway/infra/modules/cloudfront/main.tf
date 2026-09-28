# Local values for CloudFront configuration
locals {
  # Rendered as a suffix so the directive keeps its existing value byte-for-byte
  # when the variable is unset — no response-headers-policy update, and no
  # CloudFront propagation, for deployments that do not set it.
  additional_connect_src = length(var.additional_connect_src) > 0 ? " ${join(" ", var.additional_connect_src)}" : ""

  # Use PriceClass_100 (US/EU only) for dev/test, PriceClass_All for prod
  price_class = var.price_class != "" ? var.price_class : (
    var.environment == "prod" ? "PriceClass_All" : "PriceClass_100"
  )

  # Origin ID for S3 bucket
  s3_origin_id = "${var.name_prefix}-frontend-origin"

  # Origin ID for ALB (API backend)
  alb_origin_id = "${var.name_prefix}-api-origin"

  # Origin ID for VPC Origin (when using internal ALB)
  vpc_origin_id = "${var.name_prefix}-vpc-api-origin"

  # Determine which origin to use for API traffic
  # VPC Origin takes precedence when enabled and configured
  api_origin_enabled = var.enable_vpc_origin || var.alb_domain_name != ""

  # The GitHub auth broker, reached through the gateway's REST API. Enabled only
  # when the API Gateway origin domain is supplied.
  broker_origin_id      = "${var.name_prefix}-broker-origin"
  broker_origin_enabled = var.broker_origin_domain_name != ""
  use_vpc_origin        = var.enable_vpc_origin && var.internal_alb_arn != ""

  # GitLab VPC Origin: enabled only when both DNS and ARN are provided
  gitlab_origin_enabled = var.gitlab_origin_dns != "" && var.gitlab_origin_arn != ""
  gitlab_origin_id      = "${var.name_prefix}-gitlab-origin"
}

# Origin Access Control for S3 (recommended over OAI)
resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "${var.name_prefix}-frontend-oac"
  description                       = "OAC for ${var.name_prefix} frontend S3 bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Response Headers Policy with security headers
resource "aws_cloudfront_response_headers_policy" "security_headers" {
  name    = "${var.name_prefix}-security-headers"
  comment = "Security headers for ${var.name_prefix} frontend"

  security_headers_config {
    # Strict-Transport-Security
    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      preload                    = true
      override                   = true
    }

    # X-Content-Type-Options
    content_type_options {
      override = true
    }

    # X-Frame-Options
    frame_options {
      frame_option = "DENY"
      override     = true
    }

    # X-XSS-Protection (legacy but still useful for older browsers)
    xss_protection {
      mode_block = true
      protection = true
      override   = true
    }

    # Referrer-Policy
    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }

    # Content-Security-Policy
    #
    # connect-src must include wss: — the chat widget opens a WebSocket to the
    # agent gateway WS API, and browsers enforce connect-src on WebSockets
    # (the browser silently blocks the connection and no Network-tab row
    # appears, only a console CSP error). Tightened to the execute-api host
    # pattern rather than a blanket wss: to keep the directive meaningful.
    # Region is hard-coded because this module is only used from the us-east-1
    # root stack today; if that changes, wire a variable through.
    #
    # additional_connect_src exists because putting the WS API behind a custom
    # domain changes its origin, and `https:` does not cover `wss:` — so a
    # deployment that does that must extend this directive or chat breaks with a
    # console-only error.
    content_security_policy {
      content_security_policy = "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self' data:; connect-src 'self' https: wss://*.execute-api.us-east-1.amazonaws.com${local.additional_connect_src}; frame-ancestors 'none'"
      override                = true
    }
  }
}

# CloudFront Function attached to every behavior that targets the API origin.
#
# Two jobs, in this order:
#   1. Strip the /api prefix so the ALB origin sees the path the gateway expects.
#   2. Delete client-supplied identity/trust headers before they reach the origin.
#
# Shared by the /api/* AND /.well-known/* behaviors (issue #3985). Both target the
# same API origin, so both need job 2; only /api/* needs job 1. Sharing one
# function is safe because the prefix match is segment-anchored — a /.well-known/*
# URI can never match it — which keeps the two behaviors from needing two
# near-identical functions that could drift apart.
resource "aws_cloudfront_function" "strip_api_prefix" {
  name    = "${var.name_prefix}-strip-api-prefix"
  runtime = "cloudfront-js-2.0"
  comment = "Strips /api prefix and drops client-supplied identity headers before the ALB origin"
  publish = true

  code = <<-EOF
    function handler(event) {
      var request = event.request;
      // Segment-anchored: only strip /api when it is a whole path segment.
      // An unanchored /^\/api/ would rewrite /apifoo -> /foo, and would also
      // corrupt URIs on any other behavior this function is attached to.
      request.uri = request.uri.replace(/^\/api(?=\/|$)/, '');
      if (request.uri === '') request.uri = '/';

      // Do not forward client-supplied identity/trust headers to the origin.
      // On the paths that reach the API origin these are set only by trusted
      // upstream infrastructure, never by the viewer, so any inbound value is
      // dropped here before the origin-request policy forwards headers. The
      // Authorization header (the viewer's bearer token) is intentionally left
      // intact, as are x-api-key and the anthropic-* client headers.
      var h = request.headers;
      delete h['x-caller-identity'];
      delete h['x-amzn-iam-user-arn'];
      delete h['x-amzn-requestcontext'];
      delete h['x-auth-source'];
      delete h['x-internal-api-key'];
      for (var name in h) {
        if (name.indexOf('x-agent-') === 0) delete h[name];
      }
      return request;
    }
  EOF
}

# CloudFront Function attached to the S3 default cache behavior ONLY.
#
# Rewrites extensionless URIs to /index.html so React Router deep links resolve
# to the app shell. This replaces the distribution-wide `custom_error_response`
# that used to turn any 403 into "200 + /index.html" — that rule could not be
# scoped to an origin, so it also masked every authorization denial the API
# origin returned (issue #4386). Doing the rewrite on viewer-request means S3 is
# only ever asked for objects that exist, so the 403 never occurs and no error
# rewrite is needed. Full rationale is in the function source.
#
# Deliberately NOT attached to /api/*, /.well-known/*, or /gitlab/* — those
# behaviors must return their origin's real status codes.
resource "aws_cloudfront_function" "spa_fallback" {
  name    = "${var.name_prefix}-spa-fallback"
  runtime = "cloudfront-js-2.0"
  comment = "Rewrites extensionless URIs to /index.html for SPA routing (S3 behavior only)"
  publish = true

  code = file("${path.module}/functions/spa-fallback.js")
}

# =============================================================================
# CloudFront VPC Origin for Internal ALB
# =============================================================================
# VPC Origins allow CloudFront to connect to private resources within a VPC.
# This is used when the ALB is internal (not internet-facing), making CloudFront
# the sole ingress point for all traffic.
#
# NOTE: The VPC Origin is typically created in the backend-deploy workflow
# after the Ingress ALB is created by EKS, since the ALB ARN is dynamic.
# This resource is here for Terraform-managed deployments where the ALB ARN
# is known at plan time.

resource "aws_cloudfront_vpc_origin" "api" {
  count = local.use_vpc_origin ? 1 : 0

  vpc_origin_endpoint_config {
    name                   = "${var.name_prefix}-api-vpc-origin"
    arn                    = var.internal_alb_arn
    http_port              = 80
    https_port             = 443
    origin_protocol_policy = "http-only"

    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-vpc-origin"
    Service = "cdn"
    Purpose = "api-vpc-origin"
  })
}

# =============================================================================
# CloudFront VPC Origin for GitLab Internal ALB
# =============================================================================
# Created only when gitlab_origin_dns and gitlab_origin_arn are both non-empty.
# Routes /gitlab/* traffic to the GitLab internal ALB via CloudFront.

resource "aws_cloudfront_vpc_origin" "gitlab" {
  count = local.gitlab_origin_enabled ? 1 : 0

  vpc_origin_endpoint_config {
    name                   = "${var.name_prefix}-gitlab-vpc-origin"
    arn                    = var.gitlab_origin_arn
    http_port              = 80
    https_port             = 443
    origin_protocol_policy = "http-only"

    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-gitlab-vpc-origin"
    Service = "cdn"
    Purpose = "gitlab-vpc-origin"
  })
}

# CloudFront Distribution
resource "aws_cloudfront_distribution" "frontend" {
  lifecycle {
    precondition {
      condition     = !var.enable_task_api_route || local.broker_origin_enabled
      error_message = "Task routing requires the published REST API origin and stage."
    }
  }
  enabled             = true
  is_ipv6_enabled     = var.enable_ipv6
  default_root_object = "index.html"
  comment             = "${var.name_prefix} frontend distribution"
  price_class         = local.price_class

  # Custom domain aliases (if provided)
  aliases = var.custom_domain_name != "" ? [var.custom_domain_name] : []

  # S3 Origin with OAC
  origin {
    domain_name              = var.s3_bucket_regional_domain_name
    origin_id                = local.s3_origin_id
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  # ALB Origin for API backend (internet-facing ALB - custom origin)
  # This is used when the ALB is public and CloudFront connects directly via domain name
  dynamic "origin" {
    for_each = var.alb_domain_name != "" && !local.use_vpc_origin ? [1] : []
    content {
      domain_name = var.alb_domain_name
      origin_id   = local.alb_origin_id

      custom_origin_config {
        http_port              = 80
        https_port             = 443
        origin_protocol_policy = "http-only"
        origin_ssl_protocols   = ["TLSv1.2"]
        # Use configured timeout for SSE streaming support (default 30s is too short)
        origin_read_timeout      = var.vpc_origin_read_timeout
        origin_keepalive_timeout = var.vpc_origin_keepalive_timeout
      }
    }
  }

  # VPC Origin for API backend (internal ALB - VPC Origin)
  # This is used when the ALB is internal and CloudFront connects via VPC Origin
  # NOTE: domain_name must be the ALB DNS name (NOT the VPC origin ARN — the
  # CloudFront API rejects ARN-shaped strings here with "origin name cannot
  # contain a colon"). The actual VPC routing is enforced by vpc_origin_config.
  dynamic "origin" {
    for_each = local.use_vpc_origin ? [1] : []
    content {
      domain_name = var.internal_alb_dns
      origin_id   = local.vpc_origin_id

      # VPC Origin specific configuration
      vpc_origin_config {
        vpc_origin_id            = aws_cloudfront_vpc_origin.api[0].id
        origin_read_timeout      = var.vpc_origin_read_timeout
        origin_keepalive_timeout = var.vpc_origin_keepalive_timeout
      }
    }
  }

  # GitHub auth broker origin — the gateway's REST API.
  #
  # A custom origin, not a VPC origin: API Gateway is a public regional endpoint,
  # reached over the internet from CloudFront's edge. origin_path carries the
  # stage, so a viewer request for /auth/github/start arrives at the API as
  # /<stage>/auth/github/start and matches its /auth/github/{proxy+} route
  # without any path rewriting.
  dynamic "origin" {
    for_each = local.broker_origin_enabled ? [1] : []
    content {
      domain_name = var.broker_origin_domain_name
      origin_id   = local.broker_origin_id
      origin_path = var.broker_origin_path

      custom_origin_config {
        http_port              = 80
        https_port             = 443
        origin_protocol_policy = "https-only"
        origin_ssl_protocols   = ["TLSv1.2"]
      }
    }
  }

  # GitLab Origin (internal ALB via VPC Origin)
  # Created only when gitlab_origin_dns and gitlab_origin_arn are both set.
  dynamic "origin" {
    for_each = local.gitlab_origin_enabled ? [1] : []
    content {
      domain_name = var.gitlab_origin_dns
      origin_id   = local.gitlab_origin_id

      vpc_origin_config {
        vpc_origin_id            = aws_cloudfront_vpc_origin.gitlab[0].id
        origin_read_timeout      = 60
        origin_keepalive_timeout = 60
      }
    }
  }

  # Only the collection endpoint uses canonical Task ingress. Exact matching
  # leaves /api/v1/tasks/<id>/events on the ALB streaming path below.
  dynamic "ordered_cache_behavior" {
    for_each = var.enable_task_api_route && local.broker_origin_enabled ? [1] : []
    content {
      path_pattern     = "/api/v1/tasks"
      allowed_methods  = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
      cached_methods   = ["GET", "HEAD"]
      target_origin_id = local.broker_origin_id

      viewer_protocol_policy   = "redirect-to-https"
      cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
      origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id

      function_association {
        event_type   = "viewer-request"
        function_arn = aws_cloudfront_function.strip_api_prefix.arn
      }
      compress = true
    }
  }

  # API Cache Behavior — proxy /api/* to ALB (no caching, forward all headers)
  # Uses either custom origin (internet-facing ALB) or VPC origin (internal ALB)
  dynamic "ordered_cache_behavior" {
    for_each = local.api_origin_enabled ? [1] : []
    content {
      path_pattern     = "/api/*"
      allowed_methods  = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
      cached_methods   = ["GET", "HEAD"]
      target_origin_id = local.use_vpc_origin ? local.vpc_origin_id : local.alb_origin_id

      viewer_protocol_policy = "redirect-to-https"

      # Disable caching for API requests (critical for SSE streaming)
      cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
      origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id

      # Strip /api prefix before forwarding to ALB
      function_association {
        event_type   = "viewer-request"
        function_arn = aws_cloudfront_function.strip_api_prefix.arn
      }

      compress = true
    }
  }

  # GitHub auth broker behaviour — proxy /auth/github/* to the REST API.
  #
  # Putting the OAuth start and callback on the same origin as the dashboard
  # means the whole login flow stays on one hostname, and no browser needs to
  # reach the API Gateway hostname directly.
  #
  # NOTE the origin request policy: AllViewerExceptHostHeader, not AllViewer.
  # API Gateway rejects a request whose Host header is not its own, so
  # forwarding the viewer's Host — which AllViewer does, and which every other
  # behaviour here wants — makes this return 403 from the API's edge with
  # nothing in the broker's logs. This is the single detail that makes an
  # API Gateway origin behind CloudFront work.
  #
  # Caching is disabled: these are OAuth redirects carrying single-use state.
  dynamic "ordered_cache_behavior" {
    for_each = local.broker_origin_enabled && var.enable_broker_cloudfront_route ? [1] : []
    content {
      path_pattern     = "/auth/github/*"
      allowed_methods  = ["GET", "HEAD", "OPTIONS"]
      cached_methods   = ["GET", "HEAD"]
      target_origin_id = local.broker_origin_id

      viewer_protocol_policy = "redirect-to-https"

      cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
      origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id

      compress = true
    }
  }

  # GitLab Cache Behavior — proxy /gitlab/* to GitLab ALB (no caching, forward all)
  dynamic "ordered_cache_behavior" {
    for_each = local.gitlab_origin_enabled ? [1] : []
    content {
      path_pattern     = "/gitlab/*"
      allowed_methods  = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
      cached_methods   = ["GET", "HEAD"]
      target_origin_id = local.gitlab_origin_id

      viewer_protocol_policy = "redirect-to-https"

      # Disable caching — GitLab serves dynamic content
      cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
      origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id

      compress = true
    }
  }

  # Well-Known Cache Behavior — proxy /.well-known/* to API origin (no caching)
  # Routes standard well-known URIs (e.g. /.well-known/jwks.json) to the backend
  # instead of the S3 default origin. No prefix stripping — backend expects the
  # full /.well-known/* path.
  dynamic "ordered_cache_behavior" {
    for_each = local.api_origin_enabled ? [1] : []
    content {
      path_pattern     = "/.well-known/*"
      allowed_methods  = ["GET", "HEAD", "OPTIONS"]
      cached_methods   = ["GET", "HEAD"]
      target_origin_id = local.use_vpc_origin ? local.vpc_origin_id : local.alb_origin_id

      viewer_protocol_policy = "redirect-to-https"

      # Disable caching — well-known responses may change (key rotation, etc.)
      cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
      origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id

      # Issue #3985: this behavior targets the same API origin as /api/* and uses
      # the same all-viewer origin-request policy, so without the function a
      # client-supplied identity header reaches the pod here too. The function's
      # prefix rewrite is segment-anchored and cannot match a /.well-known/* URI,
      # so attaching it strips headers without touching the path.
      function_association {
        event_type   = "viewer-request"
        function_arn = aws_cloudfront_function.strip_api_prefix.arn
      }

      compress = true
    }
  }

  # Default Cache Behavior
  default_cache_behavior {
    allowed_methods  = ["GET", "HEAD", "OPTIONS"]
    cached_methods   = ["GET", "HEAD"]
    target_origin_id = local.s3_origin_id

    # Viewer protocol policy: redirect-to-https
    viewer_protocol_policy = "redirect-to-https"

    # Use CachingOptimized managed cache policy
    cache_policy_id = data.aws_cloudfront_cache_policy.caching_optimized.id

    # Use response headers policy for security headers
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security_headers.id

    # SPA deep-link routing: rewrite extensionless URIs to /index.html before the
    # S3 fetch. Scoped to this behavior so API status codes are never touched.
    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.spa_fallback.arn
    }

    # Compress automatically
    compress = true
  }

  # NO `custom_error_response` BLOCK — this is deliberate. Do not add one.
  #
  # `custom_error_response` is distribution-wide: it cannot be scoped to a cache
  # behavior or an origin. A `403 → 200 /index.html` rule here (removed in issue
  # #4386) rewrote authorization denials from the API origin into successful-looking
  # SPA HTML, so clients, acceptance tests, and security monitors could not tell a
  # refused request from an allowed one. The sibling `404` rule was removed earlier
  # for the same reason (semantic API 404s were being turned into HTML).
  #
  # SPA routing is handled instead by `aws_cloudfront_function.spa_fallback` on the
  # S3 default behavior above. `tests/infra/test_cloudfront_spa_fallback.py` guards
  # both halves of this arrangement.

  # Viewer certificate (custom domain or CloudFront default)
  viewer_certificate {
    cloudfront_default_certificate = var.custom_domain_name == ""
    acm_certificate_arn            = var.custom_domain_name != "" ? var.acm_certificate_arn : null
    ssl_support_method             = var.custom_domain_name != "" ? "sni-only" : null
    minimum_protocol_version       = var.custom_domain_name != "" ? "TLSv1.2_2021" : "TLSv1"
  }

  # Restrictions (no geo restrictions by default)
  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # Logging configuration (optional)
  dynamic "logging_config" {
    for_each = var.log_bucket_domain_name != "" ? [1] : []
    content {
      bucket          = var.log_bucket_domain_name
      prefix          = var.log_prefix
      include_cookies = false
    }
  }

  # WAF Web ACL association (optional)
  web_acl_id = var.waf_web_acl_arn != "" ? var.waf_web_acl_arn : null

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-cloudfront"
    Service = "cdn"
    Purpose = "frontend-distribution"
  })

  # Wait for OAC to be created before distribution
  depends_on = [aws_cloudfront_origin_access_control.frontend]
}

# Data source for AWS managed CachingOptimized cache policy
data "aws_cloudfront_cache_policy" "caching_optimized" {
  name = "Managed-CachingOptimized"
}

# Data source for AWS managed CachingDisabled cache policy (for API proxy)
data "aws_cloudfront_cache_policy" "caching_disabled" {
  name = "Managed-CachingDisabled"
}

# Data source for AWS managed AllViewer origin request policy (forwards all headers/cookies/query strings)
data "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "Managed-AllViewer"
}

# Forwards everything except Host. Required for the API Gateway origin: API
# Gateway 403s a request carrying someone else's Host header.
data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}
