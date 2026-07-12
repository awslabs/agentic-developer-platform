# Local values for CloudFront configuration
locals {
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
  use_vpc_origin     = var.enable_vpc_origin && var.internal_alb_arn != ""

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
    content_security_policy {
      content_security_policy = "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self' data:; connect-src 'self' https: wss://*.execute-api.us-east-1.amazonaws.com; frame-ancestors 'none'"
      override                = true
    }
  }
}

# CloudFront Function to strip /api prefix before forwarding to ALB origin
resource "aws_cloudfront_function" "strip_api_prefix" {
  name    = "${var.name_prefix}-strip-api-prefix"
  runtime = "cloudfront-js-2.0"
  comment = "Strips /api prefix from URI before forwarding to ALB"
  publish = true

  code = <<-EOF
    function handler(event) {
      var request = event.request;
      request.uri = request.uri.replace(/^\/api/, '');
      if (request.uri === '') request.uri = '/';
      return request;
    }
  EOF
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
  enabled             = true
  is_ipv6_enabled     = true
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

    # Compress automatically
    compress = true
  }

  # Custom error response for SPA routing (403 → /index.html with 200)
  # NOTE: Only 403 is needed — S3 with OAC returns 403 (Access Denied) for
  # non-existent objects, not 404. Removing the 404 rule ensures API endpoints
  # can return semantic 404 responses without CloudFront rewriting them to HTML.
  custom_error_response {
    error_caching_min_ttl = 10
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
  }

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
