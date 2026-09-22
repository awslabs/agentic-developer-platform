# =============================================================================
# GitLab CE Infrastructure — Internal Application Load Balancer
# =============================================================================
# Internal ALB in private subnets. Terminates TLS and forwards HTTP to GitLab.
# =============================================================================

# -----------------------------------------------------------------------------
# Internal ALB
# -----------------------------------------------------------------------------

resource "aws_lb" "gitlab" {
  name               = "${local.name_prefix}-alb"
  internal           = true
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = local.private_subnets

  enable_deletion_protection = var.environment == "prod"

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-alb"
    Service = "load-balancer"
  })
}

# -----------------------------------------------------------------------------
# Target Group (HTTP:80 → GitLab instance)
# -----------------------------------------------------------------------------

resource "aws_lb_target_group" "gitlab" {
  name     = "${local.name_prefix}-tg"
  port     = 80
  protocol = "HTTP"
  vpc_id   = local.vpc_id

  target_type = "instance"

  health_check {
    enabled             = true
    healthy_threshold   = 3
    interval            = 30
    matcher             = "200"
    path                = "/-/health"
    port                = "traffic-port"
    protocol            = "HTTP"
    timeout             = 10
    unhealthy_threshold = 3
  }

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-tg"
    Service = "load-balancer"
  })
}

# -----------------------------------------------------------------------------
# Target Group Attachment
# -----------------------------------------------------------------------------

resource "aws_lb_target_group_attachment" "gitlab" {
  target_group_arn = aws_lb_target_group.gitlab.arn
  target_id        = aws_instance.gitlab.id
  port             = 80
}

# -----------------------------------------------------------------------------
# HTTPS Listener (443 → target group)
# -----------------------------------------------------------------------------

resource "aws_lb_listener" "https" {
  count = var.certificate_arn != "" ? 1 : 0

  load_balancer_arn = aws_lb.gitlab.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gitlab.arn
  }

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-listener-https"
    Service = "load-balancer"
  })
}

# -----------------------------------------------------------------------------
# HTTP Listener (80 → forward to target group)
# -----------------------------------------------------------------------------
# CloudFront is the sole ingress on port 80. No redirect needed — forward all
# traffic directly to the GitLab target group.
# -----------------------------------------------------------------------------

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.gitlab.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gitlab.arn
  }

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-listener-http"
    Service = "load-balancer"
  })
}

# -----------------------------------------------------------------------------
# Deny public access to GitLab's operational endpoints (Issue #5685)
# -----------------------------------------------------------------------------
# GitLab serves /-/metrics (Prometheus counters), /-/metrics/system (runtime and
# worker details), /-/readiness (per-component database/cache/Gitaly/Sidekiq
# health detail) and /-/liveness. Those describe the server's internals, not
# anyone's code, and nothing in front of this ALB authenticates them: the
# CloudFront /gitlab/* behavior forwards every method and every viewer header to
# this ALB with no auth and no path filtering, so before these rules an anonymous
# internet request could read them at https://<cloudfront-domain>/gitlab/-/metrics.
#
# Why the rules match the /gitlab-PREFIXED form only:
#
# GitLab runs in relative_url_root mode (see user_data.sh), so the public path
# through CloudFront carries the /gitlab prefix and CloudFront does NOT strip it
# (the /gitlab/* behavior has no prefix-stripping function, unlike /api/*).
# Approved operators reach GitLab directly over the private network by its
# Route53 name and HTTPS listener. Relative-root mode still applies there, so
# their actual monitoring routes are also /gitlab-prefixed (for example,
# `curl https://<gitlab_domain>/gitlab/-/readiness`). The deny rules therefore
# belong only to the HTTP listener used by the CloudFront VPC origin; the
# VPC-only HTTPS listener must continue forwarding these routes.
#
# Why this CANNOT break the ALB health check:
#
# The target-group health check is issued by the load balancer nodes straight to
# the target; it never traverses listener rules, so no rule can intercept it. It
# also probes the unprefixed /-/health, which nginx answers directly via
# custom_gitlab_server_config before the request reaches GitLab — independent of
# both these rules and GitLab's monitoring_whitelist.
#
# Why narrowly scoped paths and not /gitlab/-/*:
#
# GitLab puts a huge amount of ordinary user-facing routing under /-/ (sign-in
# at /users/sign_in, but also /-/profile, /-/user_settings, project routes like
# /<group>/<project>/-/tree/<ref> and /-/merge_requests/<n>). A wildcard would
# break normal browsing, so each restricted endpoint is listed separately. The
# only wildcard is a route-local `.*`, because Rails accepts an optional format
# suffix (for example /-/readiness.json). Git fetch/push
# (/<group>/<project>.git/*) and webhook delivery match none of these patterns.

locals {
  # Operational endpoints that must not be readable by anonymous public callers.
  # Keep every Rails route separate: a /gitlab/-/* wildcard would also block
  # ordinary application routes, while /-/metrics/system is not covered by an
  # exact /-/metrics match. Rails adds an optional (.:format) suffix to each
  # route, so the route-local `.*` pattern is required to cover forms such as
  # /gitlab/-/readiness.json without widening the match to unrelated /-/ paths.
  gitlab_restricted_ops_endpoints = {
    metrics = {
      priority = 10
      paths = [
        "/gitlab/-/metrics",
        "/gitlab/-/metrics/",
        "/gitlab/-/metrics.*",
      ]
    }
    metrics_system = {
      priority = 11
      paths = [
        "/gitlab/-/metrics/system",
        "/gitlab/-/metrics/system/",
        "/gitlab/-/metrics/system.*",
      ]
    }
    readiness = {
      priority = 12
      paths = [
        "/gitlab/-/readiness",
        "/gitlab/-/readiness/",
        "/gitlab/-/readiness.*",
      ]
    }
    liveness = {
      priority = 13
      paths = [
        "/gitlab/-/liveness",
        "/gitlab/-/liveness/",
        "/gitlab/-/liveness.*",
      ]
    }
  }
}

resource "aws_lb_listener_rule" "deny_public_ops_http" {
  for_each = local.gitlab_restricted_ops_endpoints

  listener_arn = aws_lb_listener.http.arn
  priority     = each.value.priority

  action {
    type = "fixed-response"

    fixed_response {
      content_type = "text/plain"
      message_body = "Forbidden"
      status_code  = "403"
    }
  }

  condition {
    path_pattern {
      # Exact path, trailing slash, and Rails' optional format suffix. Query
      # strings are not part of a path-pattern match, so /-/readiness?all=1 is
      # covered by the bare path.
      values = each.value.paths
    }
  }

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-deny-public-${each.key}-http"
    Service = "load-balancer"
  })
}
